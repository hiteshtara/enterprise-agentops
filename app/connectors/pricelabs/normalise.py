"""The only module that knows PriceLabs field names.

Everything downstream speaks `app.connectors.pricelabs.models`. When the
runtime transport is decided -- the REST API, an MCP proxy, an export -- the
fetch changes and this translation does not.

Field semantics are the provider's own, confirmed against the PriceLabs
knowledge base on 2026-09-04:

  ``price``               Final Price. What PriceLabs shows on the calendar and
                          recommends pushing to the PMS. This is the figure the
                          board prices inventory with.
  ``user_price``          Customized Price: the uncustomized price with the
                          account's customizations applied. It is **not** the
                          value currently live on the PMS.
  ``uncustomized_price``  The algorithm's neighbourhood-demand price, before
                          customizations.

The value actually synced to the PMS ("Last Seen Price") is not present in this
payload at all, so nothing here may claim to show what a channel is serving.

``unbookable`` means the date is not bookable because of a stay restriction --
a minimum stay, or a check-in/check-out rule. A night's ``date`` is the night
stayed, not a checkout date, so no checkout-day rule from any other provider is
applied to it.

``occupancy`` arrives as a **number, not an int**: the REST API returns ``1.0``
where the MCP returned ``1``, and a single listing can mix the two within one
response. Verified live 2026-09-04 against all seven listings. An int-only
check here classified every REST night as UNKNOWN.
"""

from datetime import date, datetime, timedelta
from typing import Any

from app.connectors.pricelabs.models import (
    ListingHealth,
    Night,
    NightState,
    PropertyCalendar,
)

#: Statuses that mean the night is sold. Compared case-insensitively by prefix
#: so "Booked" and "Booked (Check-In)" both land here.
BOOKED_PREFIX = "booked"

BLOCKED_STATUS = "blocked"

#: PriceLabs writes -1 into numeric fields that do not apply to a row. A
#: sentinel is not a price, and must never be summed as one.
NOT_APPLICABLE = -1


def _is_number(raw: Any) -> bool:
    """A real number. `True` is an int in Python and is not a measurement."""
    return isinstance(raw, (int, float)) and not isinstance(raw, bool)


def _price(raw: Any) -> float | None:
    """A usable nightly price, or None. A sentinel or a non-number is None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None

    if raw <= 0 or raw == NOT_APPLICABLE:
        return None

    return float(raw)


def _minimum_stay(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None

    return raw if raw > 0 else None


def night_state(row: dict[str, Any]) -> NightState:
    """Classify one night.

    Written so that every path that is not positively established lands on
    UNKNOWN. An unrecognised status, an absent occupancy, or an unreadable
    ``unbookable`` flag is an unknown night -- never an open one.
    """
    if "booking_status" not in row or "occupancy" not in row:
        return NightState.UNKNOWN

    status = row.get("booking_status")
    occupancy = row.get("occupancy")

    if not isinstance(status, str) or not _is_number(occupancy):
        return NightState.UNKNOWN

    normalised = status.strip().lower()

    if normalised == BLOCKED_STATUS:
        return NightState.BLOCKED

    if normalised.startswith(BOOKED_PREFIX) or occupancy == 1:
        return NightState.BOOKED

    if normalised:
        # A status we do not recognise. Not established as vacant.
        return NightState.UNKNOWN

    unbookable = row.get("unbookable")

    if not _is_number(unbookable):
        return NightState.UNKNOWN

    if unbookable == 1:
        return NightState.UNBOOKABLE

    if unbookable == 0:
        return NightState.OPEN

    return NightState.UNKNOWN


def _stay_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None

    try:
        return date.fromisoformat(raw)

    except ValueError:
        return None


def listing_health(payload: dict[str, Any], month: str) -> ListingHealth | None:
    """Pacing for one month from a ``listing_health_and_recommendations`` body.

    Returns None rather than an empty shell when the payload carries nothing
    usable, so the console can say "no pacing data" instead of showing zeroes.
    """
    data = payload.get("data")

    if not isinstance(data, dict):
        return None

    rows = data.get("market_section")

    matched: list[Any] | None = None

    if isinstance(rows, list):
        for row in rows:
            if (
                isinstance(row, list)
                and row
                and str(row[0]).lower().startswith(month.lower())
            ):
                matched = row

                break

    market, listing = _occupancies(matched)
    low, high = _booking_window(matched)

    heading = data.get("heading_section")

    flag = None

    if isinstance(heading, dict) and isinstance(heading.get("flag"), str):
        flag = heading["flag"]

    recommendations = data.get("recommendation_section")

    headers: tuple[str, ...] = ()

    if isinstance(recommendations, dict):
        headers = tuple(
            entry["header"]
            for entry in recommendations.values()
            if isinstance(entry, dict) and isinstance(entry.get("header"), str)
        )

    health = ListingHealth(
        month_label=str(matched[0]) if matched else None,
        is_month_scoped=matched is not None,
        market_occupancy_pct=market,
        listing_occupancy_pct=listing,
        booking_window_min_days=low,
        booking_window_max_days=high,
        provider_flag=flag,
        provider_recommendations=headers,
    )

    empty = (
        market is None
        and listing is None
        and low is None
        and high is None
        and flag is None
        and not headers
    )

    return None if empty else health


def _numbers(text: str) -> list[float]:
    digits: list[float] = []

    current = ""

    for char in text:
        if char.isdigit() or (char == "." and current):
            current += char

        else:
            if current:
                digits.append(float(current))

            current = ""

    if current:
        digits.append(float(current))

    return digits


def _occupancies(row: list[Any] | None) -> tuple[float | None, float | None]:
    """Market and listing occupancy from PriceLabs' prose market line.

    The provider ships this as a sentence, not as fields. Parsing prose is
    fragile by nature, so anything that does not yield exactly the two numbers
    the sentence promises is reported as unknown rather than guessed at.
    """
    if not row or len(row) < 2 or not isinstance(row[1], str):
        return None, None

    found = _numbers(row[1])

    if len(found) != 2:
        return None, None

    market, listing = found

    if not (0 <= market <= 100 and 0 <= listing <= 100):
        return None, None

    return market, listing


def _booking_window(row: list[Any] | None) -> tuple[int | None, int | None]:
    if not row or len(row) < 4 or not isinstance(row[3], str):
        return None, None

    found = _numbers(row[3])

    if len(found) != 2:
        return None, None

    low, high = int(found[0]), int(found[1])

    if low < 0 or high < low:
        return None, None

    return low, high


def property_calendar(
    payload: dict[str, Any],
    display_name: str,
    start: date,
    horizon_days: int,
    health: ListingHealth | None = None,
) -> PropertyCalendar:
    """Translate one ``get_listing_prices`` body into a calendar.

    Only nights inside the horizon are kept, and a night the payload never
    mentions is counted as missing rather than silently treated as open.
    """
    entries = payload.get("data")

    if not isinstance(entries, list) or not entries:
        raise ValueError("PriceLabs payload carried no listing data")

    listing = entries[0] if isinstance(entries[0], dict) else None

    if listing is None:
        raise ValueError("PriceLabs payload carried an unreadable listing")

    rows = listing.get("data")

    wanted = {start + timedelta(days=offset) for offset in range(horizon_days)}

    nights: dict[date, Night] = {}

    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue

            stay_date = _stay_date(row.get("date"))

            if stay_date is None or stay_date not in wanted:
                continue

            nights[stay_date] = Night(
                stay_date=stay_date,
                state=night_state(row),
                price=_price(row.get("price")),
                minimum_stay=_minimum_stay(row.get("min_stay")),
            )

    ordered = tuple(nights[day] for day in sorted(wanted) if day in nights)

    return PropertyCalendar(
        listing_id=str(listing.get("id") or "unknown"),
        display_name=display_name,
        nights=ordered,
        currency=str(listing.get("currency") or "USD"),
        last_refreshed_at=_refreshed(listing.get("last_refreshed_at")),
        health=health,
        missing_night_count=len(wanted) - len(ordered),
    )


def _refreshed(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None

    try:
        datetime.fromisoformat(raw)

    except ValueError:
        return None

    return raw


#: Horizon -> the pair of fields the listings endpoint exposes for it. These
#: are native numbers on the payload, not prose, and they line up exactly with
#: the horizons the board offers. Verified live 2026-09-04.
OCCUPANCY_FIELDS: dict[int, tuple[str, str]] = {
    7: ("occupancy_next_7", "market_occupancy_next_7"),
    30: ("occupancy_next_30", "market_occupancy_next_30"),
    60: ("occupancy_next_60", "market_occupancy_next_60"),
}


def _percentage(raw: Any) -> float | None:
    """A percentage from PriceLabs' "86 %" spelling, or None.

    Anything that is not a plain percentage is unknown rather than coerced.
    """
    if _is_number(raw):
        value = float(raw)

    elif isinstance(raw, str):
        digits = _numbers(raw)

        if len(digits) != 1:
            return None

        value = digits[0]

    else:
        return None

    return value if 0 <= value <= 100 else None


def listing_health_from_listing(
    row: dict[str, Any],
    horizon_days: int,
) -> ListingHealth | None:
    """Pacing for one listing from the REST listings row.

    The REST portfolio endpoint carries occupancy and market occupancy as
    fields, for exactly the horizons this board offers, so nothing is parsed
    out of prose here.

    It carries no booking-window figure, so `booking_window_*` stay None. That
    is a real gap and it degrades honestly: a window with no pacing data earns
    no ranking bonus rather than a default one.
    """
    fields = OCCUPANCY_FIELDS.get(horizon_days)

    if fields is None:
        return None

    listing_field, market_field = fields

    listing = _percentage(row.get(listing_field))
    market = _percentage(row.get(market_field))

    if listing is None and market is None:
        return None

    return ListingHealth(
        month_label=f"Next {horizon_days} days",
        market_occupancy_pct=market,
        listing_occupancy_pct=listing,
    )


#: The percentile series the neighbourhood payload publishes, by label. Read
#: from `Labels` rather than by position -- the order is the provider's to
#: change, and a silently reordered series would misprice every night.
MARKET_SERIES = {
    "25th Percentile": "p25",
    "50th Percentile": "p50",
    "75th Percentile": "p75",
    "90th Percentile": "p90",
    "Median Booked Price": "booked_median",
    "N_Bookings": "n_bookings",
}

OCCUPANCY_SERIES = {"Occupancy": "market_occupancy"}


def _series(block: Any, bedrooms: int | None) -> tuple[list[str], dict[str, list]]:
    """(dates, {label: series}) for the closest bedroom category."""
    if not isinstance(block, dict):
        return [], {}

    labels = block.get("Labels")
    categories = block.get("Category")

    if not isinstance(labels, list) or not isinstance(categories, dict):
        return [], {}

    key = None

    if bedrooms is not None and str(bedrooms) in categories:
        key = str(bedrooms)

    else:
        numeric = sorted(
            (k for k in categories if str(k).isdigit()),
            key=lambda k: abs(int(k) - (bedrooms or 0)),
        )

        key = numeric[0] if numeric else None

    if key is None:
        return [], {}

    entry = categories[key]

    if not isinstance(entry, dict):
        return [], {}

    dates = entry.get("X_values")
    values = entry.get("Y_values")

    if not isinstance(dates, list) or not isinstance(values, list):
        return [], {}

    def flatten(series: Any) -> list:
        """Occupancy series arrive wrapped one level deeper than prices.

        Unwrapped by shape rather than by block, so neither layout silently
        yields a series of lists where numbers were expected.
        """
        if (
            isinstance(series, list)
            and len(series) == 1
            and isinstance(series[0], list)
        ):
            return series[0]

        return series if isinstance(series, list) else []

    return dates, {
        label: flatten(values[index])
        for index, label in enumerate(labels)
        if index < len(values)
    }


def parse_market_series(
    payload: dict[str, Any],
    bedrooms: int | None,
) -> dict[str, dict[str, float | None]]:
    """Per-stay-date market readings, keyed by ISO date.

    Anything the payload does not carry is absent rather than defaulted, so a
    missing series degrades a recommendation's confidence instead of quietly
    inventing a benchmark.
    """
    rows: dict[str, dict[str, float | None]] = {}

    dates, series = _series(payload.get("Future Percentile Prices"), bedrooms)

    for index, day in enumerate(dates):
        if not isinstance(day, str):
            continue

        entry: dict[str, float | None] = {}

        for label, name in MARKET_SERIES.items():
            values = series.get(label)

            if isinstance(values, list) and index < len(values):
                value = values[index]

                entry[name] = float(value) if isinstance(value, (int, float)) else None

        rows[day] = entry

    occ_dates, occ_series = _series(payload.get("Future Occ/New/Canc"), bedrooms)

    for index, day in enumerate(occ_dates):
        if not isinstance(day, str) or day not in rows:
            continue

        for label, name in OCCUPANCY_SERIES.items():
            values = occ_series.get(label)

            if isinstance(values, list) and index < len(values):
                value = values[index]

                rows[day][name] = (
                    float(value) if isinstance(value, (int, float)) else None
                )

    return rows
