"""Deterministic vacancy and pricing analysis.

Every number on the Vacancy board is computed here, in Python, from provider
calendars. No language model participates in classification, scoring, or
ranking: the same calendars always produce the same board, and every figure can
be traced to a rule in this module.

The rules, stated once so the console and the tests agree with the code:

* **Sellable** means `NightState.OPEN` only. Unbookable, blocked and unknown
  nights are vacant-but-not-sellable and are counted separately. They are never
  added to a sellable total.
* **Occupancy counts an owner-blocked night as occupied**, not as inventory
  removed from the denominator. See `OCCUPIED_STATES` -- this is PriceLabs'
  own arithmetic, established by measurement, not a preference.
* **A night with no price contributes no revenue.** It is counted, reported,
  and excluded from every sum -- a missing price never becomes zero.
* **High value is property-relative** (`HIGH_VALUE_PERCENTILE` of that
  property's own priced nights in the horizon), never one portfolio-wide
  dollar threshold.
* **Weekend** means the Friday or Saturday night.
* **Ranking is a published formula** (`score_window`) over data already on the
  window. Ties break on start date, then listing id, so the order is total.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.connectors.pricelabs.models import (
    ListingHealth,
    Night,
    NightState,
    PropertyCalendar,
)

#: The horizons the board offers. A closed set: an arbitrary window would make
#: percentile baselines incomparable between one view and the next.
ALLOWED_HORIZONS: tuple[int, ...] = (7, 30, 60)

DEFAULT_HORIZON = 60

#: A night counts as high value when its price reaches this percentile of its
#: own property's priced nights in the horizon.
HIGH_VALUE_PERCENTILE = 75.0

#: Below this many priced nights a percentile is noise, so the property gets no
#: high-value classification at all rather than a meaningless one.
MIN_PRICED_NIGHTS_FOR_PERCENTILE = 8

#: A property is "below market" only by this many percentage points or more.
#: Smaller gaps are inside the noise of a monthly market estimate.
MATERIAL_OCCUPANCY_GAP_POINTS = 10.0

#: An open run at least this long is worth flagging on its own.
LARGE_WINDOW_NIGHTS = 5

#: Nights that count as occupied.
#:
#: A `BLOCKED` night is in here because PriceLabs counts it that way, which was
#: settled by measuring all seven listings against the provider's own figure on
#: 2026-09-04. Roslindale: 40 booked, 2 blocked, 60 nights.
#:
#:     booked / total              = 66.7%
#:     booked / (total - blocked)  = 69.0%
#:     (booked + blocked) / total  = 70.0%   <- PriceLabs reports 70%
#:
#: Only the third reading reproduces it, and the other six listings (which have
#: no blocked nights) agree with all three. So an owner-blocked night is
#: unavailable inventory that counts as occupied -- it is not vacancy, and it
#: does not depress occupancy.
#:
#: `UNBOOKABLE` is deliberately *not* here. It stays in the denominator as
#: not-occupied, which Modern Condo confirms: 48 booked of 60 nights with 4
#: unbookable reports 80%, so its unbookable nights are counted as unsold
#: inventory rather than removed.
OCCUPIED_STATES: frozenset[NightState] = frozenset(
    {NightState.BOOKED, NightState.BLOCKED}
)

#: A calendar month needs at least this many nights inside the horizon before
#: its occupancy is treated as a signal. A three-night sliver of the month the
#: horizon happens to end in is an artefact of the window, not a weak month.
MONTH_MIN_NIGHTS_FOR_SIGNAL = 7

#: How far a month must fall below the property's own rolling occupancy across
#: the horizon before it is called out.
MATERIAL_MONTH_GAP_POINTS = 10.0

#: A month holding at least this share of a property's open value is flagged as
#: concentrated, provided the month is a real one by the rule above.
CONCENTRATED_MONTH_SHARE = 0.5

# -- ranking weights ------------------------------------------------------
# Each is a fraction of the window's own gross value, so every term is in the
# same unit and the score stays explainable as "value, adjusted".
WEEKEND_BONUS_PER_NIGHT = 0.05
WEEKEND_BONUS_CAP = 0.20
HIGH_VALUE_BONUS = 0.10
BOOKING_WINDOW_BONUS = 0.15
BELOW_MARKET_BONUS = 0.20

TOP_OPPORTUNITIES = 10


def occupancy_of(nights: Sequence[Night]) -> tuple[float | None, int, int]:
    """(occupancy %, occupied nights, denominator) for `nights`.

    The denominator is every night whose state is known: an unknown night is
    not evidence either way, so it leaves the calculation entirely rather than
    being counted as vacant.
    """
    known = [night for night in nights if night.state is not NightState.UNKNOWN]

    if not known:
        return None, 0, 0

    occupied = sum(1 for night in known if night.state in OCCUPIED_STATES)

    return round(100.0 * occupied / len(known), 1), occupied, len(known)


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolation percentile over a sorted copy of `values`.

    Spelled out rather than taken from `statistics` so the board, the tests and
    this docstring cannot drift apart on which of several definitions is used.
    """
    if not values:
        return None

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    rank = (pct / 100.0) * (len(ordered) - 1)

    low = math.floor(rank)
    high = math.ceil(rank)

    if low == high:
        return ordered[low]

    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50.0)


def priced(nights: Sequence[Night]) -> list[float]:
    """Every usable price among `nights`. Nights without one are simply absent."""
    return [night.price for night in nights if night.price is not None]


def gross_value(nights: Sequence[Night]) -> float:
    """Total ask across `nights`. Unpriced nights add nothing, not zero-dollars.

    The distinction matters upstream: callers pair this with `len(priced(...))`
    so a partial total is never presented as a complete one.
    """
    return sum(priced(nights))


def average_daily_rate(nights: Sequence[Night]) -> float | None:
    """Gross over *priced* nights. None when nothing in the run has a price."""
    prices = priced(nights)

    if not prices:
        return None

    return sum(prices) / len(prices)


def high_value_threshold(calendar: PropertyCalendar) -> float | None:
    """The property's own high-value cutoff, or None when it cannot be set.

    The baseline is every priced night in the horizon, booked ones included:
    the question is where this night sits in what the property is worth, not
    in what happens to still be for sale.
    """
    prices = priced(calendar.nights)

    if len(prices) < MIN_PRICED_NIGHTS_FOR_PERCENTILE:
        return None

    return percentile(prices, HIGH_VALUE_PERCENTILE)


def runs_of(
    nights: Sequence[Night],
    state: NightState,
) -> list[list[Night]]:
    """Maximal consecutive runs of `state`, in date order.

    Consecutive means calendar-adjacent, so a night missing from the source
    ends a run rather than joining two runs that never touched.
    """
    found: list[list[Night]] = []

    current: list[Night] = []

    for night in nights:
        adjacent = bool(current) and (night.stay_date - current[-1].stay_date).days == 1

        if night.state is state and (not current or adjacent):
            current.append(night)

            continue

        if current:
            found.append(current)

        current = [night] if night.state is state else []

    if current:
        found.append(current)

    return found


@dataclass(frozen=True)
class Window:
    """A run of consecutive nights in one state, priced and described."""

    listing_id: str
    display_name: str
    start: date
    end: date
    nights: int
    weekend_nights: int
    priced_nights: int
    gross: float
    adr: float | None
    high_value_nights: int
    minimum_stays: tuple[int, ...]
    truncated: bool
    prices: tuple[float | None, ...]

    @property
    def has_complete_pricing(self) -> bool:
        return self.priced_nights == self.nights


def describe_window(
    calendar: PropertyCalendar,
    run: Sequence[Night],
    horizon_start: date,
    horizon_end: date,
    threshold: float | None,
) -> Window:
    prices = priced(run)

    stays = tuple(sorted({night.minimum_stay for night in run if night.minimum_stay}))

    return Window(
        listing_id=calendar.listing_id,
        display_name=calendar.display_name,
        start=run[0].stay_date,
        end=run[-1].stay_date,
        nights=len(run),
        weekend_nights=sum(1 for night in run if night.is_weekend),
        priced_nights=len(prices),
        gross=sum(prices),
        adr=average_daily_rate(run),
        high_value_nights=count_high_value(run, threshold),
        minimum_stays=stays,
        truncated=(
            run[0].stay_date == horizon_start or run[-1].stay_date == horizon_end
        ),
        prices=tuple(night.price for night in run),
    )


def count_high_value(
    nights: Sequence[Night],
    threshold: float | None,
) -> int:
    if threshold is None:
        return 0

    return sum(
        1 for night in nights if night.price is not None and night.price >= threshold
    )


def unbookable_reason(window: Window) -> str | None:
    """Why the provider's data shows this run as unsellable, or None.

    Only stated when the provider's own numbers establish it: a minimum stay
    longer than the gap is a fact on the payload, not an inference about the
    account's settings. When no minimum stay came back, this says nothing.
    """
    if not window.minimum_stays:
        return None

    longest = max(window.minimum_stays)

    if longest <= window.nights:
        return None

    return f"{longest}-night minimum against a {window.nights}-night gap"


MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def month_breakdown(
    calendar: PropertyCalendar,
    horizon_start: date,
    horizon_end: date,
) -> list[dict]:
    """Occupancy and open value per calendar month inside the horizon.

    This exists because a rolling 30- or 60-day average hides a weak month: a
    property can sit at its market rate across the window while one month
    inside it is half empty. The nightly rows already carry everything needed,
    so no second source is involved.

    A month at either end of the horizon is usually partial. `covers_month` and
    `is_signal` say so, and a partial sliver never becomes a finding.
    """
    grouped: dict[tuple[int, int], list[Night]] = {}

    for night in calendar.nights:
        grouped.setdefault(
            (night.stay_date.year, night.stay_date.month),
            [],
        ).append(night)

    property_open = gross_value(
        [n for n in calendar.nights if n.state is NightState.OPEN]
    )

    months: list[dict] = []

    for (year, month), nights in sorted(grouped.items()):
        occupancy, occupied, denominator = occupancy_of(nights)

        open_nights = [n for n in nights if n.state is NightState.OPEN]

        open_gross = gross_value(open_nights)

        first = nights[0].stay_date
        last = nights[-1].stay_date

        # Whether the horizon actually contains the whole month, rather than
        # clipping it at either end.
        covers_month = (
            first.day == 1
            and (last + timedelta(days=1)).month != month
            and first >= horizon_start
            and last <= horizon_end
        )

        months.append(
            {
                "year": year,
                "month": month,
                "label": f"{MONTH_NAMES[month - 1]} {year}",
                "start": first.isoformat(),
                "end": last.isoformat(),
                "nights_counted": len(nights),
                "occupied_nights": occupied,
                "denominator_nights": denominator,
                "booked_nights": sum(
                    1 for n in nights if n.state is NightState.BOOKED
                ),
                "blocked_nights": sum(
                    1 for n in nights if n.state is NightState.BLOCKED
                ),
                "open_sellable_nights": len(open_nights),
                "unbookable_nights": sum(
                    1 for n in nights if n.state is NightState.UNBOOKABLE
                ),
                "unknown_nights": sum(
                    1 for n in nights if n.state is NightState.UNKNOWN
                ),
                "occupancy_pct": occupancy,
                "open_gross_value": round(open_gross, 2),
                "share_of_open_value": (
                    None
                    if property_open <= 0
                    else round(open_gross / property_open, 4)
                ),
                "covers_month": covers_month,
                "is_signal": len(nights) >= MONTH_MIN_NIGHTS_FOR_SIGNAL,
            }
        )

    return months


def month_market_occupancy(
    health: ListingHealth | None,
    month: dict,
) -> float | None:
    """The market figure for this month, or None.

    Only returned when the provider's own figure is month-scoped and names this
    month. The REST portfolio endpoint reports rolling 7/30/60-day occupancy,
    which is not a month and is never borrowed as one -- a rolling benchmark
    printed beside a month's occupancy would be a comparison nobody made.
    """
    if health is None or not health.is_month_scoped:
        return None

    label = health.month_label or ""

    if not label.lower().startswith(MONTH_NAMES[month["month"] - 1].lower()):
        return None

    return health.market_occupancy_pct


@dataclass(frozen=True)
class PropertyContext:
    """Per-property facts the ranking is allowed to consider."""

    below_market: bool
    occupancy_gap_points: float | None
    booking_window_max_days: int | None

    def inside_booking_window(self, lead_days: int) -> bool:
        """Whether a stay this far out is inside how this property books.

        Unknown pacing is not "inside": an absent booking window earns no
        ranking bonus rather than a default one.
        """
        if self.booking_window_max_days is None:
            return False

        return lead_days <= self.booking_window_max_days


def property_context(calendar: PropertyCalendar) -> PropertyContext:
    health = calendar.health

    market = health.market_occupancy_pct if health else None
    listing = health.listing_occupancy_pct if health else None

    gap = None if market is None or listing is None else market - listing

    return PropertyContext(
        below_market=gap is not None and gap >= MATERIAL_OCCUPANCY_GAP_POINTS,
        occupancy_gap_points=gap,
        booking_window_max_days=(health.booking_window_max_days if health else None),
    )


def score_window(
    window: Window,
    context: PropertyContext,
    lead_days: int,
) -> tuple[float, list[str]]:
    """The published ranking formula, with the reasons that produced it.

    ``score = gross x (1 + weekend + high-value + pacing + below-market)``

    Every multiplier is a documented constant above, every input is already on
    the window, and nothing consults a model. A window with no priced nights
    scores zero and is reported separately rather than ranked on a guess.
    """
    if window.gross <= 0:
        return 0.0, []

    reasons: list[str] = [f"${window.gross:,.0f} open value"]

    multiplier = 1.0

    if window.weekend_nights:
        multiplier += min(
            WEEKEND_BONUS_PER_NIGHT * window.weekend_nights,
            WEEKEND_BONUS_CAP,
        )

        label = "night" if window.weekend_nights == 1 else "nights"

        reasons.append(f"{window.weekend_nights} weekend {label}")

    if window.high_value_nights:
        multiplier += HIGH_VALUE_BONUS

        label = "night" if window.high_value_nights == 1 else "nights"

        reasons.append(f"{window.high_value_nights} high-value {label}")

    if context.inside_booking_window(lead_days):
        multiplier += BOOKING_WINDOW_BONUS

        reasons.append("inside normal booking window")

    if context.below_market:
        multiplier += BELOW_MARKET_BONUS

        reasons.append("occupancy below market")

    return round(window.gross * multiplier, 2), reasons


#: Distinguishes "this caller has no reason field" from "the reason is None".
#: An orphan window always carries the key, even when nothing established a
#: reason, so a consumer reads null rather than hitting a missing key.
_NO_REASON = object()


def _window_dict(window: Window, reason: object = _NO_REASON) -> dict:
    payload = {
        "listing_id": window.listing_id,
        "display_name": window.display_name,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "nights": window.nights,
        "weekend_nights": window.weekend_nights,
        "priced_nights": window.priced_nights,
        "gross_value": round(window.gross, 2) if window.priced_nights else None,
        "adr": round(window.adr, 2) if window.adr is not None else None,
        "high_value_nights": window.high_value_nights,
        "minimum_stays": list(window.minimum_stays),
        "truncated": window.truncated,
        "complete_pricing": window.has_complete_pricing,
    }

    if reason is not _NO_REASON:
        payload["reason"] = reason

    return payload


def build_board(
    calendars: Sequence[PropertyCalendar],
    horizon_days: int,
    horizon_start: date,
    source_name: str,
    source_is_live: bool,
) -> dict:
    """The whole Vacancy board, computed from `calendars`.

    Returns plain dicts; `app.main` splats them into response models, which is
    how every other service in this codebase reaches the HTTP boundary.
    """
    if horizon_days not in ALLOWED_HORIZONS:
        raise ValueError(
            f"Unsupported horizon: {horizon_days}. "
            f"Choose one of {', '.join(str(day) for day in ALLOWED_HORIZONS)}."
        )

    horizon_end = date.fromordinal(horizon_start.toordinal() + horizon_days - 1)

    properties: list[dict] = []
    open_windows: list[tuple[Window, PropertyContext]] = []
    orphan_windows: list[dict] = []
    high_value: list[dict] = []
    attention: list[dict] = []

    for calendar in calendars:
        nights = calendar.nights
        threshold = high_value_threshold(calendar)
        context = property_context(calendar)

        counts = {state: 0 for state in NightState}

        for night in nights:
            counts[night.state] += 1

        known = len(nights) - counts[NightState.UNKNOWN]

        sellable = [n for n in nights if n.state is NightState.OPEN]
        unsellable = [n for n in nights if n.state is NightState.UNBOOKABLE]

        for run in runs_of(nights, NightState.OPEN):
            window = describe_window(
                calendar,
                run,
                horizon_start,
                horizon_end,
                threshold,
            )

            open_windows.append((window, context))

        for run in runs_of(nights, NightState.UNBOOKABLE):
            window = describe_window(
                calendar,
                run,
                horizon_start,
                horizon_end,
                threshold,
            )

            entry = _window_dict(window, reason=unbookable_reason(window))

            entry["orphan_class"] = (
                "one_night"
                if window.nights == 1
                else "two_night"
                if window.nights == 2
                else "longer"
            )

            entry["high_value"] = window.high_value_nights > 0

            entry["prices"] = list(window.prices)

            orphan_windows.append(entry)

        if threshold is not None:
            property_median = median(priced(nights))

            for night in nights:
                if night.state is not NightState.OPEN or night.price is None:
                    continue

                if night.price < threshold:
                    continue

                above = (
                    None
                    if not property_median
                    else round(
                        (night.price / property_median - 1.0) * 100.0,
                        1,
                    )
                )

                high_value.append(
                    {
                        "listing_id": calendar.listing_id,
                        "display_name": calendar.display_name,
                        "stay_date": night.stay_date.isoformat(),
                        "price": night.price,
                        "threshold": round(threshold, 2),
                        "pct_above_median": above,
                        "is_weekend": night.is_weekend,
                    }
                )

        health = calendar.health

        properties.append(
            {
                "listing_id": calendar.listing_id,
                "display_name": calendar.display_name,
                "currency": calendar.currency,
                "last_refreshed_at": calendar.last_refreshed_at,
                "nights_counted": len(nights),
                "nights_missing": calendar.missing_night_count,
                "booked_nights": counts[NightState.BOOKED],
                "open_sellable_nights": counts[NightState.OPEN],
                "unbookable_nights": counts[NightState.UNBOOKABLE],
                "blocked_nights": counts[NightState.BLOCKED],
                "unknown_nights": counts[NightState.UNKNOWN],
                "occupancy_pct": (
                    None
                    if known <= 0
                    else round(100.0 * counts[NightState.BOOKED] / known, 1)
                ),
                "sellable_gross_value": round(gross_value(sellable), 2),
                "sellable_priced_nights": len(priced(sellable)),
                "unbookable_gross_value": round(gross_value(unsellable), 2),
                "high_value_threshold": (
                    None if threshold is None else round(threshold, 2)
                ),
                "median_price": (
                    None
                    if median(priced(nights)) is None
                    else round(median(priced(nights)), 2)
                ),
                "market_occupancy_pct": (
                    health.market_occupancy_pct if health else None
                ),
                "listing_occupancy_pct": (
                    health.listing_occupancy_pct if health else None
                ),
                "booking_window_min_days": (
                    health.booking_window_min_days if health else None
                ),
                "booking_window_max_days": (
                    health.booking_window_max_days if health else None
                ),
                "provider_flag": health.provider_flag if health else None,
                "provider_recommendations": (
                    list(health.provider_recommendations) if health else []
                ),
                "calendar": [
                    {
                        "stay_date": night.stay_date.isoformat(),
                        "state": night.state.value,
                        "price": night.price,
                        "minimum_stay": night.minimum_stay,
                        "is_weekend": night.is_weekend,
                    }
                    for night in nights
                ],
            }
        )

        reasons = attention_reasons(calendar, context, horizon_start)

        if reasons:
            attention.append(
                {
                    "listing_id": calendar.listing_id,
                    "display_name": calendar.display_name,
                    "reasons": reasons,
                    # The gap travels for display, but whether it counts as
                    # "below market" is decided here against the documented
                    # threshold. A property above its market has a negative gap
                    # and must never be labelled as trailing one.
                    "below_market": context.below_market,
                    "occupancy_gap_points": (
                        None
                        if context.occupancy_gap_points is None
                        else round(context.occupancy_gap_points, 1)
                    ),
                    "listing_occupancy_pct": (
                        health.listing_occupancy_pct if health else None
                    ),
                    "market_occupancy_pct": (
                        health.market_occupancy_pct if health else None
                    ),
                    "month_label": health.month_label if health else None,
                    "provider_recommendations": (
                        list(health.provider_recommendations) if health else []
                    ),
                }
            )

    ranked = rank_opportunities(open_windows, horizon_start)

    totals = _totals(properties)

    return {
        "horizon_days": horizon_days,
        "start_date": horizon_start.isoformat(),
        "end_date": horizon_end.isoformat(),
        "source": source_name,
        "source_is_live": source_is_live,
        "generated_from_nights": sum(p["nights_counted"] for p in properties),
        "summary": totals,
        "properties": properties,
        "unbookable_windows": sorted(
            orphan_windows,
            key=lambda entry: (
                -(entry["gross_value"] or 0.0),
                entry["start"],
                entry["listing_id"],
            ),
        ),
        "open_windows": [
            _window_dict(window)
            for window, _ in sorted(
                open_windows,
                key=lambda pair: (
                    -pair[0].gross,
                    pair[0].start,
                    pair[0].listing_id,
                ),
            )
        ],
        "high_value_nights": sorted(
            high_value,
            key=lambda entry: (-entry["price"], entry["stay_date"]),
        ),
        "needs_attention": attention,
        "opportunities": ranked,
    }


def attention_reasons(
    calendar: PropertyCalendar,
    context: PropertyContext,
    horizon_start: date,
) -> list[str]:
    """Why this property needs a look, in the provider's own terms.

    Never a price recommendation of our own. When PriceLabs itself supplies a
    recommendation it is carried through by name, and that is the only pricing
    advice this board shows.
    """
    reasons: list[str] = []

    health = calendar.health

    if context.below_market and health is not None:
        reasons.append(
            f"{health.month_label or 'This period'}: occupancy "
            f"{health.listing_occupancy_pct:.0f}% against a market at "
            f"{health.market_occupancy_pct:.0f}%"
        )

    longest = max(
        (len(run) for run in runs_of(calendar.nights, NightState.OPEN)),
        default=0,
    )

    if longest >= LARGE_WINDOW_NIGHTS:
        for run in runs_of(calendar.nights, NightState.OPEN):
            if len(run) != longest:
                continue

            lead = (run[0].stay_date - horizon_start).days

            # The booking window sharpens this, but a long unsold run is worth
            # surfacing on its own. Sources differ in what they expose: the
            # REST portfolio endpoint carries no booking window at all, and a
            # section that silently empties itself on a thinner source is worse
            # than one that says less. The pacing clause is added only when
            # pacing is actually known -- never implied.
            reason = (
                f"{longest} consecutive open nights from "
                f"{run[0].stay_date.isoformat()}"
            )

            if context.inside_booking_window(lead):
                reason += (
                    f", inside the {context.booking_window_max_days}-day "
                    "booking window"
                )

            reasons.append(reason)

            break

    if health is not None:
        reasons.extend(
            f"PriceLabs recommends: {header}"
            for header in health.provider_recommendations
        )

    return reasons


def rank_opportunities(
    windows: Sequence[tuple[Window, PropertyContext]],
    horizon_start: date,
) -> list[dict]:
    """The top windows by `score_window`, ordered totally and reproducibly."""
    scored: list[tuple[float, Window, list[str]]] = []

    for window, context in windows:
        lead = (window.start - horizon_start).days

        score, reasons = score_window(window, context, lead)

        if score <= 0:
            continue

        scored.append((score, window, reasons))

    scored.sort(
        key=lambda entry: (
            -entry[0],
            entry[1].start,
            entry[1].listing_id,
        )
    )

    ranked: list[dict] = []

    for position, (score, window, reasons) in enumerate(
        scored[:TOP_OPPORTUNITIES],
        start=1,
    ):
        entry = _window_dict(window)

        entry["rank"] = position
        entry["score"] = score
        entry["reasons"] = reasons
        entry["lead_days"] = (window.start - horizon_start).days

        ranked.append(entry)

    return ranked


def _totals(properties: Sequence[dict]) -> dict:
    def total(key: str) -> int:
        return sum(entry[key] for entry in properties)

    booked = total("booked_nights")
    counted = total("nights_counted")
    unknown = total("unknown_nights")

    known = counted - unknown

    return {
        "properties": len(properties),
        "nights_counted": counted,
        "nights_missing": total("nights_missing"),
        "booked_nights": booked,
        "open_sellable_nights": total("open_sellable_nights"),
        "unbookable_nights": total("unbookable_nights"),
        "blocked_nights": total("blocked_nights"),
        "unknown_nights": unknown,
        "occupancy_pct": None if known <= 0 else round(100.0 * booked / known, 1),
        "sellable_gross_value": round(
            sum(entry["sellable_gross_value"] for entry in properties),
            2,
        ),
        "unbookable_gross_value": round(
            sum(entry["unbookable_gross_value"] for entry in properties),
            2,
        ),
    }
