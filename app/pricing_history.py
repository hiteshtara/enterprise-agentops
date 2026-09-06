"""Realized booking history, in the shape the recommendation engine expects.

`PricingRecommendationService.build(history=...)` has always accepted
`{listing_id: [(days_out_at_booking, realized_adr), ...]}` and computed
per-lead-band medians from it. Nothing supplied it, so `history_adr` was always
None and the LOWER branch was unreachable in production. This module fills that
gap and changes nothing else: the lead-band medians, the `>= 3 samples` rule
and the LOWER formula are untouched.

Where the numbers come from
---------------------------
`GET /v1/reservation_data`, verified read-only against this account on
2026-09-06:

    days_out_at_booking = date(check_in) - date(booked_date)
    realized_adr        = rental_revenue / no_of_days

`rental_revenue` is room revenue and is the right numerator. `total_cost` is
not: on a cancelled row it reads 0.0 while `rental_revenue` does not, and on a
booked row it exceeds `rental_revenue` because it carries cleaning fees and
taxes. Its meaning changes with status, so it is never used here.

Reconciled with the earlier evidence
------------------------------------
The dynamic-floor backtests used `nightly_amount` from the PriceLabs MCP
bookings report. Across all 383 reservations present in both sources, matched
on exact `reservation_id`: nights agreed 383/383, status agreed 383/383, and
the ADR difference was **never $1** -- median $0.08, p90 $0.24, max $0.45. The
gap is rounding: REST returns `rental_revenue` to whole dollars where the MCP
report does not. So live LOWER decisions rest on the same evidence those
analyses did.

What is excluded, and why nothing is repaired
---------------------------------------------
A row must be a realized stay with a computable rate. Anything else is dropped
rather than patched, because a repaired value would be an invented booking:

  * `booking_status != "booked"` -- the authoritative field. `cancelled_on` is
    **not** usable: every cancelled row on this account carries the epoch
    sentinel `1970-01-01T00:00:00.000Z`, so filtering on it would exclude
    nothing at all.
  * `rental_revenue <= 0` -- 22 booked rows, mostly `manual` channel, are
    owner blocks carried as reservations. A $0 ADR is not a cheap booking and
    would drag a median down.
  * `no_of_days <= 0`, or dates that do not parse.
  * negative lead time -- 3 rows have `booked_date` after `check_in`. Not
    clamped to zero: whatever produced them is not a same-day booking, and
    guessing which it is would put fiction into the floor evidence.
"""

import datetime
from typing import Any

from app.connectors.pricelabs.errors import PriceLabsUnavailable

#: How far back to ask for reservations. Wide on purpose -- the account's
#: earliest `booked_date` is 2024-10-20 -- because the engine's lead bands are
#: only as trustworthy as the number of samples behind them.
HISTORY_LOOKBACK_DAYS = 730

#: Reservations are fetched up to this far ahead as well: a booking made today
#: for next spring is evidence about lead time now, not in six months.
HISTORY_LOOKAHEAD_DAYS = 400


def _date(value: Any) -> datetime.date | None:
    """A date from either a plain date or an ISO timestamp. None if neither."""
    if not isinstance(value, str) or not value:
        return None

    try:
        # `fromisoformat` accepts the trailing Z directly on 3.11+.
        return datetime.datetime.fromisoformat(value).date()

    except ValueError:
        pass

    try:
        return datetime.date.fromisoformat(value[:10])

    except ValueError:
        return None


def _positive_number(value: Any) -> float | None:
    """`rental_revenue` arrives as a string. Zero and negative are not values."""
    try:
        number = float(value)

    except (TypeError, ValueError):
        return None

    return number if number > 0 else None


def history_sample(row: dict[str, Any]) -> tuple[int, float] | None:
    """One `(days_out_at_booking, realized_adr)` pair, or None if unusable.

    Every rejection is a deliberate exclusion listed in the module docstring.
    Nothing is defaulted, clamped or repaired.
    """
    if row.get("booking_status") != "booked":
        return None

    revenue = _positive_number(row.get("rental_revenue"))

    if revenue is None:
        return None

    nights = row.get("no_of_days")

    if not isinstance(nights, int) or nights <= 0:
        return None

    booked = _date(row.get("booked_date"))
    arrival = _date(row.get("check_in"))

    if booked is None or arrival is None:
        return None

    days_out = (arrival - booked).days

    if days_out < 0:
        return None

    return days_out, revenue / nights


def build_history(
    client,
    listing_ids: list[str],
    pms: str = "lodgify",
    today: datetime.date | None = None,
) -> dict[str, list[tuple[int, float]]]:
    """Realized history per listing, ready for `build(history=...)`.

    Raises `PriceLabsUnavailable` rather than returning a partial map. A
    half-loaded history is worse than none: the engine cannot tell the
    difference between "this property has no bookings in that band" and "we
    failed to fetch them", and the first answer suppresses a LOWER while the
    second silently invents the same suppression from a network error.
    """
    reference = today or datetime.datetime.now(tz=datetime.UTC).date()

    start = reference - datetime.timedelta(days=HISTORY_LOOKBACK_DAYS)
    end = reference + datetime.timedelta(days=HISTORY_LOOKAHEAD_DAYS)

    history: dict[str, list[tuple[int, float]]] = {}

    for listing_id in listing_ids:
        rows = client.reservations(
            listing_id,
            pms,
            start.isoformat(),
            end.isoformat(),
        )

        samples = [
            sample
            for sample in (history_sample(row) for row in rows)
            if sample is not None
        ]

        if samples:
            history[listing_id] = samples

    return history


def load_history(
    client,
    listing_ids: list[str],
    pms: str = "lodgify",
    today: datetime.date | None = None,
) -> tuple[dict[str, list[tuple[int, float]]] | None, str | None]:
    """History, or `(None, reason)` when the provider could not supply it.

    The `None` is load-bearing and is why this is separate from
    `build_history`. An empty dict and a failed fetch are different facts:
    `{}` means these properties genuinely have no usable bookings, while a
    failure means we do not know. Returning `{}` for a failure would let the
    caller report "history loaded" about evidence it never saw -- and since
    absent history makes LOWER unreachable, that failure would look exactly
    like a considered decision not to lower a price.
    """
    try:
        return build_history(client, listing_ids, pms, today), None

    except PriceLabsUnavailable as exc:
        return None, f"Booking history could not be loaded: {exc}"
