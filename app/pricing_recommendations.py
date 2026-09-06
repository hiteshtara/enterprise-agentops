"""Builds pricing recommendations from live PriceLabs state.

Pure orchestration: it reads, computes with `app.pricing_policy`, and returns
payloads. It writes nothing and it holds no credential.

The decision rules are the ones validated against the real portfolio on
2026-09-04, with every threshold derived from the observed distribution rather
than chosen:

  * **Materially above/below market** is the comp set's own interquartile range
    for that exact date, which PriceLabs publishes per night. No fixed
    percentage is involved.
  * **Portfolio strength** is an occupancy lead of `OCC_GAP_PTS`. The seven
    listings' leads are +2, +8, +24, +29, +30, +32, +41 -- a 16-point empty
    band sits between +8 and +24, so any threshold inside it partitions the
    portfolio identically. The midpoint is used for robustness.
  * **Date strength** is market occupancy or 7-day pickup at or above the 75th
    percentile of those readings across the open nights, or an explicit strong
    demand signal. `events` is deliberately *not* a gate: it fires on a third
    of all nights, which makes it a label rather than evidence.
  * **Inside `NEAR_TERM` days** filling beats positioning, so portfolio-level
    strength cannot raise a price; only the date's own strength can, and a
    price above what that property historically converts at in that lead band
    is lowered toward it.

Near-term precedence: history-backed LOWER pre-empts RAISE
----------------------------------------------------------
Inside `NEAR_TERM_DAYS`, one night can satisfy both rules at once, and it
happens often. "Priced below the comp set's p25" and "priced above what this
unit itself converts at" are not contradictory -- they mean the market is
asking more than this property has ever actually achieved.

The two answer different questions:

    market p25                  what are comparable listings asking?
    historical lead-band ADR    what has *this* property actually converted
                                at, this close to arrival?

Close to arrival and under weak demand, the property's own realized history
wins. Filling the night is worth more than holding a position the unit has not
historically been able to sell.

This is deliberate policy, not statement order. It was written when the LOWER
branch was unreachable -- nothing supplied `history`, so `history_adr` was
always None and the question never arose -- and it is recorded here because
supplying real history made it live: on the live board it flipped four
near-term nights from RAISE to LOWER.

The precedence is narrow. LOWER may pre-empt RAISE **only** when every one of
its own conditions already holds: inside `NEAR_TERM_DAYS`, enough samples in
the matching lead band for a median, a current price exceeding that median by
`HIST_OVER`, and demand in `WEAK_DEMAND`. The guardrails then still apply --
hard floor, owner floor, `MAX_CHANGE_PER_RUN`. If any of that is absent,
history does not suppress the RAISE.

And it does not make historical ADR a target. The executable proposal remains
the cap-safe single step; the ADR is evidence about this property, nothing
more.
"""

import datetime
import statistics
from typing import Any

from app.pricing_config import bands_for
from app.pricing_policy import (
    Confidence,
    MarketState,
    PriceAction,
    Recommendation,
    cap_safe_price,
    finalise,
    to_payload,
)

OCC_GAP_PTS = 16.0
DATE_OCC_STRONG = 45.4
DATE_PICKUP_STRONG = 8.9
NEAR_TERM_DAYS = 14
HIST_OVER = 1.15

STRONG_DEMAND = frozenset({"High Demand", "Good Demand"})
WEAK_DEMAND = frozenset({"Low Demand", "Normal Demand"})


def date_strength(
    market_occupancy: float | None,
    pickup: float | None,
    demand: str | None,
) -> list[str]:
    """The date-level strength signals that fired, named. Empty means none.

    Returning the signals rather than a boolean is deliberate: a reason that
    cites market occupancy when the signal was actually pickup is a reason
    nobody can check.
    """
    signals: list[str] = []

    if demand in STRONG_DEMAND:
        signals.append(demand.lower())

    if market_occupancy is not None and market_occupancy >= DATE_OCC_STRONG:
        signals.append(
            f"market occupancy {market_occupancy:.0f}% "
            f"(>= p75 {DATE_OCC_STRONG:.0f}%)"
        )

    if pickup is not None and pickup >= DATE_PICKUP_STRONG:
        signals.append(f"7-day pickup {pickup:.1f} (>= p75 {DATE_PICKUP_STRONG})")

    return signals


def confidence_for(
    state: MarketState,
    history_count: int,
) -> Confidence:
    """Evidence availability, and nothing else."""
    if (
        state.market_p25 is None
        or state.market_booked_median is None
        or state.market_occupancy is None
        or state.current_price is None
    ):
        return Confidence.LOW

    if history_count >= 3 and state.demand:
        return Confidence.HIGH

    return Confidence.MEDIUM


def recommend_night(
    listing_id: str,
    display_name: str,
    stay_date: datetime.date,
    days_out: int,
    state: MarketState,
    occupancy_gap: float | None,
    history_adr: float | None,
    history_count: int,
    lead_band: str,
    is_pinned: bool,
    is_open: bool,
) -> Recommendation:
    """One night, one decision. Deterministic in every branch."""
    bands = bands_for(listing_id)

    slug = bands.slug if bands else listing_id

    confidence = confidence_for(state, history_count)

    signals = date_strength(state.market_occupancy, state.pickup_7_days, state.demand)

    def build(
        action: PriceAction,
        proposed: float | None,
        reason: str,
        conflict: bool = False,
    ):
        return finalise(
            Recommendation(
                market_signal_conflict=conflict,
                listing_id=listing_id,
                slug=slug,
                display_name=display_name,
                stay_date=stay_date,
                days_out=days_out,
                action=action,
                current_price=state.current_price,
                proposed_price=proposed,
                confidence=confidence,
                reason=reason,
                state=state,
                bands=bands,
            )
        )

    if is_pinned:
        if not is_open:
            return build(
                PriceAction.KEEP_PIN,
                state.current_price,
                "Night already booked at the pinned rate — the pin did its job.",
            )

        if signals and state.market_p25 and state.current_price is not None and (
            state.current_price < state.market_p25
        ):
            return build(
                PriceAction.REMOVE_PIN,
                None,
                (
                    f"Still open and pinned at ${state.current_price:.0f} against "
                    f"a market p25 of ${state.market_p25:.0f}; this date is "
                    f"strong: {', '.join(signals)}. Removing the override "
                    "returns the night to PriceLabs dynamic pricing."
                ),
            )

        return build(
            PriceAction.KEEP_PIN,
            state.current_price,
            f"{days_out}d out, {(state.demand or 'demand unknown').lower()} — "
            "the pin is still working to fill this night.",
        )

    if not is_open or state.current_price is None:
        return build(PriceAction.HOLD, state.current_price, "Not open inventory.")

    if state.market_p25 is None or state.market_booked_median is None:
        return build(PriceAction.HOLD, state.current_price, "No market reference.")

    price = state.current_price

    if days_out <= NEAR_TERM_DAYS:
        lowers_on_history = bool(
            history_adr
            and price > history_adr * HIST_OVER
            and state.demand in WEAK_DEMAND
        )

        raises_on_market = bool(price < state.market_p25 and signals)

        # Both rules qualifying is a real state, not an error: the market is
        # asking more than this property has achieved. Recorded so a reader
        # can see it was a decision; it never changes which branch runs.
        conflict = lowers_on_history and raises_on_market

        if lowers_on_history:
            reason = (
                f"{days_out}d out and still open; asking ${price:.0f} against "
                f"${history_adr:.0f} that this property converts at "
                f"{lead_band} out (n={history_count}); "
                f"{(state.demand or '').lower()}."
            )

            if conflict:
                reason += (
                    " Near-term property history supports lowering, while the "
                    "market comparison also supports a higher rate. Because "
                    "this night is close to arrival and demand is weak, the "
                    "property's realized lead-time history takes precedence."
                )

            return build(
                PriceAction.LOWER,
                cap_safe_price(price, max(history_adr, price * 0.90)),
                reason,
                conflict=conflict,
            )

        if raises_on_market:
            return build(
                PriceAction.RAISE,
                cap_safe_price(price, min(state.market_p25, price * 1.10)),
                (
                    f"{days_out}d out, but this date is strong: "
                    f"{', '.join(signals)}; asking ${price:.0f} is below the "
                    f"market p25 of ${state.market_p25:.0f}."
                ),
            )

        return build(
            PriceAction.HOLD,
            price,
            f"{days_out}d out; near-term inventory held to fill.",
        )

    if price < state.market_p25 and (
        (occupancy_gap is not None and occupancy_gap >= OCC_GAP_PTS) or signals
    ):
        basis = (
            f"occupancy leads the market by {occupancy_gap:.0f} points"
            if occupancy_gap is not None and occupancy_gap >= OCC_GAP_PTS
            else f"date-level strength: {', '.join(signals)}"
        )

        return build(
            PriceAction.RAISE,
            cap_safe_price(price, min(state.market_p25, price * 1.10)),
            (
                f"Asking ${price:.0f} is below the market p25 of "
                f"${state.market_p25:.0f} (booked median "
                f"${state.market_booked_median:.0f}); {basis}."
            ),
        )

    return build(
        PriceAction.HOLD,
        price,
        "Within the market's own p25–p75 range for this date.",
    )


def payloads(recommendations: list[Recommendation]) -> list[dict[str, Any]]:
    """API projection, actionable first, then by size of the move."""
    ordered = sorted(
        recommendations,
        key=lambda r: (
            not r.is_actionable,
            -abs((r.proposed_price or 0) - (r.current_price or 0)),
            r.stay_date,
        ),
    )

    return [to_payload(r) for r in ordered]


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None
