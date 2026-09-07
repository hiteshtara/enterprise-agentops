"""Observational record of what happened after an executed pricing action.

This module answers one question and refuses to answer a different one it
would be easy to mistake it for. It records: *a price was changed, and then
these things were observed about that night*. It does not record, and offers
no column that could hold, a claim that the change caused any of them.

Three properties carry the design.

**First-booking evidence is immutable.** Once a qualifying post-action booking
is observed, `first_*` is frozen. A later cancellation must not erase it,
because "never booked" and "booked after the action, later cancelled" are
materially different outcomes, and 45% of reservations in this account cancel
(450 of 995, observed 2026-09-06). The guard is a conditional UPDATE --
`WHERE first_booked_at IS NULL` -- so overwriting is impossible rather than
merely avoided.

**Unknown stays unknown.** Every outcome column is nullable and null means
*we do not know*: not yet reconciled, or the provider could not be read. A
provider failure leaves the row untouched rather than writing a `False` that
would read as "did not book".

**The cancellation timestamp is ours, not the provider's.** PriceLabs returns
`cancelled_on` as the Unix epoch sentinel on 445 of 450 cancelled reservations
(observed 2026-09-06), so the event time is simply unavailable. What we can
honestly record is when *we noticed*, which is why the column is named
`cancellation_first_observed_at`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from app.database import Database, get_database
from app.db_models import PricingActionOutcomeRecord

#: Days after the *reservation's* check-out before a row may be finalized. A
#: multi-night stay covering our night is not settled until it ends, so this
#: is measured from check-out rather than from the stay date.
#:
#: **This is an owner/system policy choice, not a measured cancellation
#: window.** It cannot be measured: `cancelled_on` is a sentinel, so the
#: distribution of cancellation lag is unavailable from this provider. Thirty
#: days is well past any plausible pre-arrival cancellation, and a status
#: change after it would be a refund or chargeback -- a different question
#: from "did the night sell". Revisit it once this table has produced our own
#: observed-flip data, which is the only way we will ever measure it.
FINALIZATION_DAYS_AFTER_CHECKOUT = 30

#: Consecutive passes that must agree before a row is finalized. Elapsed time
#: alone is not the gate: a row still changing is never finalized merely
#: because a clock ran out.
FINALIZATION_STABLE_PASSES = 2

#: Passes required in total, so a row is never finalized on a single reading.
FINALIZATION_MIN_PASSES = 3


class BookingStatus(str):
    """What is true of the night now. Null is a fourth case: unknown."""

    BOOKED = "booked"
    CANCELLED = "cancelled"
    NONE = "none"


@dataclass(frozen=True)
class BookingObservation:
    """One reservation, as reconciliation understands it.

    Constructed from the connector's already-minimal projection. Carries no
    guest field and no confirmation code, because the connector cannot supply
    one.
    """

    reservation_id: str
    booked_at: str
    status: str
    realized_stay_adr: float | None
    channel: str | None
    check_out: str | None


class PricingOutcomeStore:
    """Reads and writes `pricing_action_outcomes`. Touches nothing else.

    In particular it holds no provider client of any kind. The reconciler
    passes observations in; this class cannot reach PriceLabs, and so cannot
    change a price, by construction rather than by policy.
    """

    def __init__(self, database: Database | None = None) -> None:
        self._database = database or get_database()

    # -- capture, at execution time ---------------------------------------

    def record_execution(
        self,
        *,
        approval_id: str,
        run_id: str,
        listing_id: str,
        stay_date: str,
        action: str,
        executed_at: str,
        write_outcome: str,
        cleanup_id: str | None = None,
        price_before: float | None = None,
        price_after: float | None = None,
        currency: str | None = None,
        days_out: int | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> PricingActionOutcomeRecord:
        """One row for one executed action. Never for a refusal.

        A refusal changed nothing, so it has no outcome to track. Recording
        one would put rows in the table for nights whose price never moved.
        """
        evidence = evidence or {}

        record = PricingActionOutcomeRecord(
            id=str(uuid.uuid4()),
            approval_id=approval_id,
            run_id=run_id,
            cleanup_id=cleanup_id,
            listing_id=listing_id,
            stay_date=stay_date,
            action=action,
            executed_at=executed_at,
            write_outcome=write_outcome,
            price_before=price_before,
            price_after=price_after,
            currency=currency,
            days_out=days_out,
            reconcile_pass_count=0,
            created_at=datetime.now(UTC).isoformat(),
            **{
                field: evidence.get(field)
                for field in EVIDENCE_FIELDS
            },
        )

        with self._database.session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            session.expunge(record)

            return record

    # -- reconciliation ----------------------------------------------------

    def note_first_booking(
        self,
        record_id: str,
        observation: BookingObservation,
        *,
        stay_date: str,
        executed_at: str,
    ) -> bool:
        """Write the first post-action booking. True if this call wrote it.

        **The write-once guard is the `WHERE` clause, not the caller.** Two
        reconcilers racing, or one replaying an old page, both issue this
        UPDATE; the row matches at most once and every later attempt is a
        no-op with `rowcount == 0`.

        Qualification is *not* decided here -- see
        `app.pricing_reconciler.qualifies` -- but the guard means a caller
        that gets qualification wrong on a second pass still cannot overwrite
        a correct first observation.
        """
        with self._database.session() as session:
            result = session.execute(
                update(PricingActionOutcomeRecord)
                .where(PricingActionOutcomeRecord.id == record_id)
                # The whole point. Never widen this clause.
                .where(PricingActionOutcomeRecord.first_booked_at.is_(None))
                .values(
                    first_booked_at=observation.booked_at,
                    first_booking_lead_days=lead_days(
                        stay_date,
                        observation.booked_at,
                    ),
                    first_hours_from_action=hours_between(
                        executed_at,
                        observation.booked_at,
                    ),
                    first_realized_stay_adr=observation.realized_stay_adr,
                    first_reservation_id=observation.reservation_id,
                    first_booking_channel=observation.channel,
                )
            )

            session.commit()

            return result.rowcount == 1

    def set_current(
        self,
        record_id: str,
        *,
        status: str,
        reservation_id: str | None = None,
        realized_stay_adr: float | None = None,
        channel: str | None = None,
        cancelled_after_booking: bool | None = None,
        cancellation_observed_at: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Overwrite what is true now. Never touches `first_*`.

        `cancellation_first_observed_at` is written only when still null, so
        it records the *first* time we noticed rather than the most recent
        pass that saw the same cancellation.
        """
        moment = (now or datetime.now(UTC)).isoformat()

        values: dict[str, Any] = {
            "current_booking_status": status,
            "current_reservation_id": reservation_id,
            "current_realized_stay_adr": realized_stay_adr,
            "current_booking_channel": channel,
            "last_reconciled_at": moment,
            "reconcile_pass_count": (
                PricingActionOutcomeRecord.reconcile_pass_count + 1
            ),
        }

        if cancelled_after_booking is not None:
            values["cancelled_after_booking"] = cancelled_after_booking

        with self._database.session() as session:
            session.execute(
                update(PricingActionOutcomeRecord)
                .where(PricingActionOutcomeRecord.id == record_id)
                .values(**values)
            )

            if cancellation_observed_at is not None:
                session.execute(
                    update(PricingActionOutcomeRecord)
                    .where(PricingActionOutcomeRecord.id == record_id)
                    .where(
                        PricingActionOutcomeRecord
                        .cancellation_first_observed_at.is_(None)
                    )
                    .values(
                        cancellation_first_observed_at=cancellation_observed_at
                    )
                )

            session.commit()

    def touch(self, record_id: str, now: datetime | None = None) -> None:
        """Record that a pass looked at this row and learned nothing new.

        Distinct from `set_current`: it advances freshness without asserting
        anything about the booking, which is what a pass over an unbooked
        night does.
        """
        moment = (now or datetime.now(UTC)).isoformat()

        with self._database.session() as session:
            session.execute(
                update(PricingActionOutcomeRecord)
                .where(PricingActionOutcomeRecord.id == record_id)
                .values(
                    last_reconciled_at=moment,
                    reconcile_pass_count=(
                        PricingActionOutcomeRecord.reconcile_pass_count + 1
                    ),
                )
            )

            session.commit()

    def finalize(self, record_id: str, now: datetime | None = None) -> None:
        """Stop routine polling. Not a claim that the row can never change."""
        moment = (now or datetime.now(UTC)).isoformat()

        with self._database.session() as session:
            session.execute(
                update(PricingActionOutcomeRecord)
                .where(PricingActionOutcomeRecord.id == record_id)
                .values(finalized_at=moment)
            )

            session.commit()

    def reopen(self, record_id: str, now: datetime | None = None) -> None:
        """A later pass disagreed with a finalized row.

        Clears `finalized_at` and records that it happened. `first_*` is not
        involved: reopening changes what is true now, never what was observed
        after the action.
        """
        moment = (now or datetime.now(UTC)).isoformat()

        with self._database.session() as session:
            session.execute(
                update(PricingActionOutcomeRecord)
                .where(PricingActionOutcomeRecord.id == record_id)
                .values(finalized_at=None, reopened_at=moment)
            )

            session.commit()

    # -- reads -------------------------------------------------------------

    def get(self, record_id: str) -> PricingActionOutcomeRecord | None:
        with self._database.session() as session:
            record = session.get(PricingActionOutcomeRecord, record_id)

            if record is not None:
                session.expunge(record)

            return record

    def open_outcomes(self) -> list[PricingActionOutcomeRecord]:
        """Rows still being polled: everything not finalized."""
        with self._database.session() as session:
            rows = list(
                session.scalars(
                    select(PricingActionOutcomeRecord)
                    .where(PricingActionOutcomeRecord.finalized_at.is_(None))
                    .order_by(PricingActionOutcomeRecord.executed_at)
                )
            )

            for row in rows:
                session.expunge(row)

            return rows

    def list_outcomes(
        self,
        listing_id: str | None = None,
        action: str | None = None,
        limit: int = 200,
    ) -> list[PricingActionOutcomeRecord]:
        statement = select(PricingActionOutcomeRecord)

        if listing_id:
            statement = statement.where(
                PricingActionOutcomeRecord.listing_id == listing_id
            )

        if action:
            statement = statement.where(
                PricingActionOutcomeRecord.action == action
            )

        statement = statement.order_by(
            PricingActionOutcomeRecord.executed_at.desc()
        ).limit(limit)

        with self._database.session() as session:
            rows = list(session.scalars(statement))

            for row in rows:
                session.expunge(row)

            return rows

    def health(self, now: datetime | None = None) -> dict[str, Any]:
        """Whether reconciliation is actually running.

        The failure this exists to catch: the job stops, rows quietly stay
        unreconciled, and the report understates everything. Without this,
        "nothing booked" and "the job died" look identical -- which is why
        the unreconciled age is reported alongside the counts rather than
        left to be inferred from them.
        """
        moment = now or datetime.now(UTC)

        with self._database.session() as session:
            total = session.scalar(
                select(func.count(PricingActionOutcomeRecord.id))
            )

            finalized = session.scalar(
                select(func.count(PricingActionOutcomeRecord.id)).where(
                    PricingActionOutcomeRecord.finalized_at.is_not(None)
                )
            )

            unreconciled = list(
                session.scalars(
                    select(PricingActionOutcomeRecord)
                    .where(
                        PricingActionOutcomeRecord.last_reconciled_at.is_(None)
                    )
                    .order_by(PricingActionOutcomeRecord.executed_at)
                )
            )

            last_success = session.scalar(
                select(func.max(PricingActionOutcomeRecord.last_reconciled_at))
            )

            awaiting = session.scalar(
                select(func.count(PricingActionOutcomeRecord.id)).where(
                    PricingActionOutcomeRecord.first_booked_at.is_(None)
                )
            )

            booked_now = session.scalar(
                select(func.count(PricingActionOutcomeRecord.id)).where(
                    PricingActionOutcomeRecord.current_booking_status
                    == BookingStatus.BOOKED
                )
            )

            cancelled_after = session.scalar(
                select(func.count(PricingActionOutcomeRecord.id)).where(
                    PricingActionOutcomeRecord.cancelled_after_booking.is_(True)
                )
            )

            reopened = session.scalar(
                select(func.count(PricingActionOutcomeRecord.id)).where(
                    PricingActionOutcomeRecord.reopened_at.is_not(None)
                )
            )

        oldest = unreconciled[0].executed_at if unreconciled else None

        return {
            "checked_at": moment.isoformat(),
            "outcomes": total or 0,
            "unreconciled": len(unreconciled),
            "oldest_unreconciled_executed_at": oldest,
            "oldest_unreconciled_age_hours": hours_between(
                oldest,
                moment.isoformat(),
            ),
            "last_successful_reconciliation_at": last_success,
            "finalized": finalized or 0,
            "awaiting_first_booking": awaiting or 0,
            "booked_currently": booked_now or 0,
            "cancelled_after_booking": cancelled_after or 0,
            "reopened": reopened or 0,
        }


#: Evidence keys copied verbatim from the recommendation payload. Listed once
#: so the model, the capture path and the projection cannot drift.
EVIDENCE_FIELDS: tuple[str, ...] = (
    "history_adr",
    "history_sample_count",
    "market_p25",
    "market_booked_median",
    "demand",
    "listing_occupancy",
    "market_occupancy",
    "market_signal_conflict",
    "hard_floor",
    "owner_floor",
    "observed_commission_rate",
)


def hours_between(start: str | None, end: str | None) -> float | None:
    """Hours from `start` to `end`, or None if either cannot be read.

    Unknown stays unknown: an unparsable timestamp returns None rather than
    zero, which would read as "immediately".
    """
    first, second = _moment(start), _moment(end)

    if first is None or second is None:
        return None

    return round((second - first).total_seconds() / 3600.0, 2)


def lead_days(stay_date: str | None, booked_at: str | None) -> int | None:
    """Whole days from booking to arrival.

    Date-based deliberately: check-in has date semantics, so expressing this
    in hours would imply precision the stay date does not carry.
    """
    stay, booked = _date_of(stay_date), _date_of(booked_at)

    if stay is None or booked is None:
        return None

    return (stay - booked).days


def _moment(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))

    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _date_of(value: str | None):
    moment = _moment(value)

    if moment is not None:
        return moment.date()

    try:
        return datetime.fromisoformat(str(value)).date()

    except (TypeError, ValueError):
        return None


def to_payload(record: PricingActionOutcomeRecord) -> dict[str, Any]:
    """The single API projection.

    **No reservation id, in either half.** Those are join keys into records
    that are personal; they are needed for write-once correctness and
    idempotent reconciliation, and for nothing a reader needs to see. There is
    no branch of this function that includes one -- the same rule that keeps
    `password_hash` out of `user_to_dict`.
    """
    return {
        "id": record.id,
        "approval_id": record.approval_id,
        "run_id": record.run_id,
        "cleanup_id": record.cleanup_id,
        "listing_id": record.listing_id,
        "stay_date": record.stay_date,
        "action": record.action,
        "executed_at": record.executed_at,
        "write_outcome": record.write_outcome,
        "price_before": record.price_before,
        "price_after": record.price_after,
        "currency": record.currency,
        "days_out": record.days_out,
        "history_adr": record.history_adr,
        "history_sample_count": record.history_sample_count,
        "market_p25": record.market_p25,
        "market_booked_median": record.market_booked_median,
        "demand": record.demand,
        "listing_occupancy": record.listing_occupancy,
        "market_occupancy": record.market_occupancy,
        "market_signal_conflict": record.market_signal_conflict,
        "hard_floor": record.hard_floor,
        "owner_floor": record.owner_floor,
        "observed_commission_rate": record.observed_commission_rate,
        "first_booked_at": record.first_booked_at,
        "first_booking_lead_days": record.first_booking_lead_days,
        "first_hours_from_action": record.first_hours_from_action,
        "first_realized_stay_adr": record.first_realized_stay_adr,
        "first_booking_channel": record.first_booking_channel,
        "current_booking_status": record.current_booking_status,
        "current_realized_stay_adr": record.current_realized_stay_adr,
        "current_booking_channel": record.current_booking_channel,
        "cancelled_after_booking": record.cancelled_after_booking,
        "last_reconciled_at": record.last_reconciled_at,
        "reconcile_pass_count": record.reconcile_pass_count,
        "cancellation_first_observed_at": record.cancellation_first_observed_at,
        "finalized_at": record.finalized_at,
        "reopened_at": record.reopened_at,
    }


__all__ = [
    "EVIDENCE_FIELDS",
    "FINALIZATION_DAYS_AFTER_CHECKOUT",
    "FINALIZATION_MIN_PASSES",
    "FINALIZATION_STABLE_PASSES",
    "BookingObservation",
    "BookingStatus",
    "PricingOutcomeStore",
    "hours_between",
    "lead_days",
    "to_payload",
]
