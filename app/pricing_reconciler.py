"""Progressive reconciliation of executed pricing actions against bookings.

**Structurally read-only against providers.** This module is constructed with
a reader and has no writer of any kind -- not an unused one, an absent one.
It cannot change a price, and that is a property of its shape rather than of
its behaviour.

Why progressive rather than one late pass
-----------------------------------------
PriceLabs returns `cancelled_on` as the Unix epoch sentinel on 445 of 450
cancelled reservations (observed 2026-09-06 across all seven listings). The
event time is therefore unavailable, and repeated observation is the *only*
mechanism by which a cancellation can be detected at all. Our polling cadence
is the entire temporal resolution we will ever have on it.

The one thing cadence does *not* limit is first-booking precision:
`booked_date` carries a full timestamp, so once a booking is seen at all we
know exactly when it was made. A booking six hours after a reduction is
recorded as 6.0 hours even under daily polling.

What counts as an outcome
-------------------------
A reservation qualifies as a post-action booking only when all three hold:

1. `booking_status == "booked"`;
2. `booked_date > executed_at` -- **a reservation booked before the action
   never counts, even if it covers the night.** That night was already sold
   when we changed the price, and counting it would manufacture a success
   rate out of nights that were never available; and
3. its occupied nights contain the stay date, using
   `check_in <= stay_date < check_out`. Check-out is exclusive, confirmed
   against the provider: `check_out - check_in == no_of_days` on every one of
   282 reservations sampled.

Nothing here is a causal claim. A booking after a price change is adjacency
in time.
"""

import datetime
from dataclasses import dataclass
from typing import Any

from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.db_models import PricingActionOutcomeRecord
from app.pricing_outcomes import (
    FINALIZATION_DAYS_AFTER_CHECKOUT,
    FINALIZATION_MIN_PASSES,
    BookingObservation,
    BookingStatus,
    PricingOutcomeStore,
)

#: How far either side of the stay dates to ask the provider for. One day of
#: slack absorbs timezone edges in the provider's own date handling.
WINDOW_PADDING_DAYS = 1


@dataclass(frozen=True)
class ReconcileOutcome:
    """What one pass concluded about one row. Reported, never inferred."""

    outcome_id: str
    listing_id: str
    stay_date: str
    status: str | None
    first_booking_recorded: bool = False
    cancellation_observed: bool = False
    finalized: bool = False
    reopened: bool = False
    detail: str = ""


def occupied_nights(row: dict[str, Any]) -> tuple[datetime.date, ...]:
    """Every night a reservation occupies. Check-out is excluded.

    `check_in` alone is not enough: a three-night stay arriving on the 8th
    occupies the 10th, and using only the arrival date would miss it.
    """
    start = _date(row.get("check_in"))
    end = _date(row.get("check_out"))

    if start is None or end is None or end <= start:
        return ()

    span = (end - start).days

    return tuple(start + datetime.timedelta(days=n) for n in range(span))


def covers(row: dict[str, Any], stay_date: str) -> bool:
    """Does this reservation occupy that night? `check_in <= d < check_out`."""
    night = _date(stay_date)

    return night is not None and night in occupied_nights(row)


def qualifies(row: dict[str, Any], stay_date: str, executed_at: str) -> bool:
    """Is this a *post-action* booking of that night?

    The `booked_date > executed_at` half is the load-bearing rule of the whole
    feature. Without it, a night sold weeks earlier would be counted as an
    outcome of a price change made after it was already gone.
    """
    if row.get("booking_status") != BookingStatus.BOOKED:
        return False

    booked = _moment(row.get("booked_date"))
    acted = _moment(executed_at)

    if booked is None or acted is None:
        return False

    if booked <= acted:
        return False

    return covers(row, stay_date)


def observation(row: dict[str, Any]) -> BookingObservation | None:
    """One reservation as an observation, or None if it cannot be read."""
    reservation_id = row.get("reservation_id")
    booked_at = row.get("booked_date")

    if not isinstance(reservation_id, str) or not reservation_id:
        return None

    if not isinstance(booked_at, str) or not booked_at:
        return None

    return BookingObservation(
        reservation_id=reservation_id,
        booked_at=booked_at,
        status=str(row.get("booking_status") or ""),
        realized_stay_adr=_stay_adr(row),
        channel=row.get("booking_channel"),
        check_out=row.get("check_out"),
    )


class PricingOutcomeReconciler:
    """One pass over the outcome rows still being polled.

    Takes a *reader* and a store. There is no writer parameter, so no code
    path through this class can reach a pricing endpoint.
    """

    def __init__(
        self,
        reader,
        outcomes: PricingOutcomeStore,
        pms: str = "lodgify",
    ) -> None:
        self._reader = reader
        self._outcomes = outcomes
        self._pms = pms

    def run_once(
        self,
        now: datetime.datetime | None = None,
    ) -> list[ReconcileOutcome]:
        """Reconcile every row due for a look. Returns what it concluded.

        **One provider fetch per listing, not per row.** Rows are grouped by
        listing and a single windowed request covers all of that listing's
        open outcomes, however many nights they span.
        """
        moment = now or datetime.datetime.now(datetime.UTC)

        due = [
            record
            for record in self._outcomes.open_outcomes()
            if _due(record, moment)
        ]

        by_listing: dict[str, list[PricingActionOutcomeRecord]] = {}

        for record in due:
            by_listing.setdefault(record.listing_id, []).append(record)

        results: list[ReconcileOutcome] = []

        for listing_id, records in by_listing.items():
            try:
                rows = self._reservations(listing_id, records)

            except PriceLabsUnavailable as exc:
                # Unknown stays unknown. The rows are left exactly as they
                # were -- no status written, no pass counted -- so a provider
                # outage can never be mistaken for "nothing booked".
                for record in records:
                    results.append(
                        ReconcileOutcome(
                            outcome_id=record.id,
                            listing_id=listing_id,
                            stay_date=record.stay_date,
                            status=record.current_booking_status,
                            detail=(
                                "PriceLabs could not be read; this row is "
                                f"unchanged and still unknown ({exc.__class__.__name__})."
                            ),
                        )
                    )

                continue

            for record in records:
                results.append(self._reconcile(record, rows, moment))

        return results

    def _reservations(
        self,
        listing_id: str,
        records: list[PricingActionOutcomeRecord],
    ) -> list[dict[str, Any]]:
        """One windowed fetch covering every open night for this listing."""
        nights = sorted(r.stay_date for r in records)

        start = _date(nights[0]) - datetime.timedelta(days=WINDOW_PADDING_DAYS)
        end = _date(nights[-1]) + datetime.timedelta(
            days=WINDOW_PADDING_DAYS + 1
        )

        return self._reader.reservations(
            listing_id,
            self._pms,
            start.isoformat(),
            end.isoformat(),
        )

    def _reconcile(
        self,
        record: PricingActionOutcomeRecord,
        rows: list[dict[str, Any]],
        moment: datetime.datetime,
    ) -> ReconcileOutcome:
        covering = [row for row in rows if covers(row, record.stay_date)]

        qualifying = [
            row
            for row in covering
            if qualifies(row, record.stay_date, record.executed_at)
        ]

        first_recorded = False

        # Earliest qualifying booking, so a night booked, cancelled and
        # rebooked still records the *first* one.
        if qualifying and record.first_booked_at is None:
            earliest = min(qualifying, key=lambda r: str(r.get("booked_date")))
            seen = observation(earliest)

            if seen is not None:
                first_recorded = self._outcomes.note_first_booking(
                    record.id,
                    seen,
                    stay_date=record.stay_date,
                    executed_at=record.executed_at,
                )

        live = [
            row
            for row in covering
            if row.get("booking_status") == BookingStatus.BOOKED
            and _moment(row.get("booked_date")) is not None
            and _moment(row.get("booked_date"))
            > _moment(record.executed_at)
        ]

        cancellation = False

        if live:
            current = min(live, key=lambda r: str(r.get("booked_date")))
            seen = observation(current)
            status = BookingStatus.BOOKED

            self._outcomes.set_current(
                record.id,
                status=status,
                reservation_id=seen.reservation_id if seen else None,
                realized_stay_adr=seen.realized_stay_adr if seen else None,
                channel=seen.channel if seen else None,
                now=moment,
            )

        elif record.first_reservation_id is not None:
            # We recorded a post-action booking earlier and nothing now
            # occupies the night: the reservation cancelled. `first_*` is
            # untouched -- that is the entire reason it exists.
            status = BookingStatus.CANCELLED
            cancellation = record.cancelled_after_booking is not True

            self._outcomes.set_current(
                record.id,
                status=status,
                cancelled_after_booking=True,
                cancellation_observed_at=moment.isoformat(),
                now=moment,
            )

        else:
            status = BookingStatus.NONE
            self._outcomes.set_current(record.id, status=status, now=moment)

        refreshed = self._outcomes.get(record.id)

        finalized = reopened = False

        if refreshed is not None and _finalizable(refreshed, covering, moment):
            self._outcomes.finalize(record.id, now=moment)
            finalized = True

        return ReconcileOutcome(
            outcome_id=record.id,
            listing_id=record.listing_id,
            stay_date=record.stay_date,
            status=status,
            first_booking_recorded=first_recorded,
            cancellation_observed=cancellation,
            finalized=finalized,
            reopened=reopened,
            detail=_describe(status, first_recorded, cancellation),
        )

    def reopen_if_changed(
        self,
        record: PricingActionOutcomeRecord,
        rows: list[dict[str, Any]],
        now: datetime.datetime | None = None,
    ) -> bool:
        """Re-check a finalized row. True if it was reopened.

        Finalization means routine polling stopped, never that the row became
        immutable. A pass that disagrees clears `finalized_at`, records
        `reopened_at`, and updates what is true now -- and still never touches
        `first_*`.
        """
        moment = now or datetime.datetime.now(datetime.UTC)

        covering = [
            row
            for row in rows
            if covers(row, record.stay_date)
            and row.get("booking_status") == BookingStatus.BOOKED
        ]

        observed = BookingStatus.BOOKED if covering else (
            BookingStatus.CANCELLED
            if record.first_reservation_id is not None
            else BookingStatus.NONE
        )

        if observed == record.current_booking_status:
            return False

        self._outcomes.reopen(record.id, now=moment)
        self._outcomes.set_current(record.id, status=observed, now=moment)

        return True


def _due(record: PricingActionOutcomeRecord, now: datetime.datetime) -> bool:
    """Cadence: daily before and during the stay, weekly after check-out.

    A row never polled is always due -- otherwise a freshly executed action
    would wait a day before anyone could tell the tracker was working.
    """
    if record.last_reconciled_at is None:
        return True

    last = _moment(record.last_reconciled_at)

    if last is None:
        return True

    stay = _date(record.stay_date)
    after_stay = stay is not None and now.date() > stay

    interval = datetime.timedelta(days=7 if after_stay else 1)

    return now - last >= interval


def _finalizable(
    record: PricingActionOutcomeRecord,
    covering: list[dict[str, Any]],
    now: datetime.datetime,
) -> bool:
    """All three conditions, and elapsed time is only one of them.

    Measured from the *reservation's* check-out where one exists: a
    multi-night stay covering our night is not settled until it ends.
    """
    if record.reconcile_pass_count < FINALIZATION_MIN_PASSES:
        return False

    checkouts = [_date(row.get("check_out")) for row in covering]
    checkouts = [day for day in checkouts if day is not None]

    anchor = max(checkouts) if checkouts else _date(record.stay_date)

    if anchor is None:
        return False

    return now.date() >= anchor + datetime.timedelta(
        days=FINALIZATION_DAYS_AFTER_CHECKOUT
    )


def _describe(status, first_recorded, cancellation) -> str:
    if cancellation:
        return (
            "The booking made after this action has cancelled. What was "
            "observed after the action is preserved; only current status "
            "changed."
        )

    if first_recorded:
        return "First booking after this action recorded."

    if status == BookingStatus.BOOKED:
        return "Still booked."

    if status == BookingStatus.NONE:
        return "No reservation occupies this night."

    return "No change."


def _stay_adr(row: dict[str, Any]) -> float | None:
    """`rental_revenue / no_of_days` -- a stay average, not a nightly rate."""
    nights = row.get("no_of_days")

    if not isinstance(nights, int) or nights <= 0:
        return None

    try:
        revenue = float(row.get("rental_revenue"))

    except (TypeError, ValueError):
        return None

    if revenue <= 0:
        return None

    return round(revenue / nights, 2)


def _date(value: Any) -> datetime.date | None:
    """Strict. No prefix salvage -- the whole string must be a date."""
    moment = _moment(value)

    if moment is not None:
        return moment.date()

    try:
        return datetime.date.fromisoformat(str(value))

    except (TypeError, ValueError):
        return None


def _moment(value: Any) -> datetime.datetime | None:
    if not value:
        return None

    try:
        # PriceLabs stamps reservations as `...T00:57:34.000Z`, which
        # `fromisoformat` handles from 3.11 on.
        parsed = datetime.datetime.fromisoformat(str(value))

    except (TypeError, ValueError):
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def summarise(outcomes: list[ReconcileOutcome]) -> dict[str, Any]:
    """What one pass did. Counts only; no claim about why anything happened."""
    return {
        "processed": len(outcomes),
        "first_bookings_recorded": sum(
            1 for o in outcomes if o.first_booking_recorded
        ),
        "cancellations_observed": sum(
            1 for o in outcomes if o.cancellation_observed
        ),
        "finalized": sum(1 for o in outcomes if o.finalized),
        "reopened": sum(1 for o in outcomes if o.reopened),
        "ran_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


__all__ = [
    "WINDOW_PADDING_DAYS",
    "PricingOutcomeReconciler",
    "ReconcileOutcome",
    "covers",
    "observation",
    "occupied_nights",
    "qualifies",
    "summarise",
]
