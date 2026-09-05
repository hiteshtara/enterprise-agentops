"""Explicit expiry for temporary fixed-price overrides.

AgentGuard removes what it wrote, rather than trusting PriceLabs'
`lead_time_expiry` to do it. See docs/PRICING_CLEANUP_V2.md for the design and
the evidence behind it.

Two properties carry the safety of this module:

**A write with no row is impossible.** The row is created before the override
is sent, because an override nobody recorded is exactly the stranded pin this
exists to prevent. `record_intent` is the only way to begin.

**Ownership is proven, never assumed.** PriceLabs gives an override no id, so
the token in `marker` -- written to the front of the provider's `reason` -- is
what ties a provider row to one record here. Cleanup refuses unless the marker,
the price and the provider timestamps all match. Every ambiguous case resolves
to NEEDS_REVIEW and sends nothing, because deleting a person's pricing decision
is unrecoverable while a queue of unresolved cleanups is merely tedious.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import PricingCleanupRecord

#: Written to the front of the provider's `reason`, so any truncation removes
#: the readable tail rather than the identity.
MARKER_PREFIX = "AGENTGUARD:"

#: The whole `reason` is capped near the only length observed surviving intact
#: (106 characters, 2026-09-04). The provider's real limit is unknown, which is
#: why `verify_marker` checks the round-trip rather than trusting this.
MAX_REASON_LENGTH = 160

#: An override may not linger more than a week, and must be gone before the
#: last two days before arrival. Settled 2026-09-05; deliberately not cleverer.
MAX_LIFETIME_DAYS = 7

DAYS_CLEAR_OF_ARRIVAL = 2


class CleanupState(str, Enum):
    """Where one temporary override stands.

    `VANISHED` is not a failure: the override is already gone and the date is
    back on dynamic pricing, which is the outcome cleanup exists to reach.

    `UNKNOWN_CLEANUP_STATE` is never retried. The override may already be gone,
    and a second DELETE against a date a person has since re-pinned would
    destroy their work.
    """

    PENDING_WRITE = "PENDING_WRITE"
    ACTIVE = "ACTIVE"
    CLEANED_UP = "CLEANED_UP"
    VANISHED = "VANISHED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNKNOWN_CLEANUP_STATE = "UNKNOWN_CLEANUP_STATE"


TERMINAL_STATES: frozenset[CleanupState] = frozenset(
    {
        CleanupState.CLEANED_UP,
        CleanupState.VANISHED,
        CleanupState.NEEDS_REVIEW,
        CleanupState.UNKNOWN_CLEANUP_STATE,
    }
)


def new_marker() -> str:
    return str(uuid.uuid4())


def build_reason(marker: str, text: str) -> str:
    """`AGENTGUARD:<marker>: <text>`, capped without ever cutting the token."""
    prefix = f"{MARKER_PREFIX}{marker}: "

    room = max(0, MAX_REASON_LENGTH - len(prefix))

    return prefix + text[:room]


def marker_of(reason: str | None) -> str | None:
    """The token carried by a provider `reason`, or None if it carries none."""
    if not isinstance(reason, str) or not reason.startswith(MARKER_PREFIX):
        return None

    rest = reason[len(MARKER_PREFIX) :]

    token, separator, _ = rest.partition(":")

    if not separator or not token:
        return None

    return token


def default_cleanup_at(
    stay_date: date,
    created_at: datetime | None = None,
) -> datetime:
    """`min(stay_date - 2 days, created_at + 7 days)`.

    The dual bound is the point: an override cannot linger more than a week,
    and is always gone before the last two days before arrival.
    """
    created = created_at or datetime.now(UTC)

    clear_of_arrival = datetime.combine(
        stay_date - timedelta(days=DAYS_CLEAR_OF_ARRIVAL),
        datetime.min.time(),
        tzinfo=UTC,
    )

    return min(clear_of_arrival, created + timedelta(days=MAX_LIFETIME_DAYS))


@dataclass(frozen=True)
class OwnershipCheck:
    """Whether a provider override is the one a record describes, and why."""

    owned: bool
    reason: str

    @property
    def refused(self) -> bool:
        return not self.owned


def check_ownership(
    record: PricingCleanupRecord,
    override: dict[str, Any] | None,
) -> OwnershipCheck:
    """Prove the provider's override is the one this record created.

    Four checks, all of which must hold. The marker is the load-bearing one:
    a person editing the date would have to reproduce a uuid they have never
    seen for a false match.

    `updated_at` is checked as a *positive* signal only. Treating
    `updated_at != created_at` as proof of tampering would rest on behaviour
    never observed from this provider -- no edited override exists in the
    account, so PriceLabs has never been seen bumping it, and may not.
    """
    if override is None:
        return OwnershipCheck(False, "the override is no longer present")

    # The one pre-V2 override carries no marker because it predates them, so
    # its ownership falls back to price and timestamps. The exemption lives on
    # the row, so it cannot spread to anything written since.
    if not record.adopted:
        found = marker_of(override.get("reason"))

        if found is None:
            return OwnershipCheck(
                False,
                "the override carries no AgentGuard marker",
            )

        if found != record.marker:
            return OwnershipCheck(
                False,
                "the override carries a marker belonging to a different record",
            )

    try:
        price = float(override.get("price"))

    except (TypeError, ValueError):
        return OwnershipCheck(False, "the override has no readable price")

    if round(price) != round(record.new_price):
        return OwnershipCheck(
            False,
            f"price is now {price:.0f}, not the {record.new_price:.0f} written",
        )

    created = override.get("created_at")

    if record.provider_created_at and created != record.provider_created_at:
        return OwnershipCheck(
            False,
            "the override was created at a different time than recorded",
        )

    if created and override.get("updated_at") != created:
        return OwnershipCheck(
            False,
            "the override has been modified since it was created",
        )

    return OwnershipCheck(True, "marker, price and timestamps all match")


class PricingCleanupStore:
    """Durable record of every temporary override awaiting cleanup."""

    def __init__(self, database: Database | None = None) -> None:
        self._database = database or get_database()

    def record_intent(
        self,
        listing_id: str,
        pms: str,
        stay_date: str,
        old_price: float | None,
        new_price: float,
        currency: str,
        cleanup_at: str,
        approval_id: str | None = None,
        run_id: str | None = None,
    ) -> PricingCleanupRecord:
        """Create the row that makes a cleanup owed. Call before writing."""
        marker = new_marker()

        record = PricingCleanupRecord(
            id=marker,
            listing_id=listing_id,
            pms=pms,
            stay_date=stay_date,
            old_price=old_price,
            new_price=new_price,
            currency=currency,
            marker=marker,
            cleanup_at=cleanup_at,
            approval_id=approval_id,
            run_id=run_id,
            state=CleanupState.PENDING_WRITE.value,
            created_at=datetime.now(UTC).isoformat(),
            adopted=False,
        )

        with self._database.session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            session.expunge(record)

        return record

    def adopt(
        self,
        listing_id: str,
        pms: str,
        stay_date: str,
        new_price: float,
        currency: str,
        cleanup_at: str,
        provider_created_at: str | None,
        approval_id: str | None,
        run_id: str | None,
        resolution: str,
    ) -> PricingCleanupRecord:
        """Take ownership of an override written before V2 existed.

        Explicit and one-off: it carries no marker, so its row is flagged
        `adopted` and ownership falls back to price and timestamps. Nothing
        else may ever take this path.
        """
        record = PricingCleanupRecord(
            id=str(uuid.uuid4()),
            listing_id=listing_id,
            pms=pms,
            stay_date=stay_date,
            old_price=None,
            new_price=new_price,
            currency=currency,
            marker=None,
            adopted=True,
            approval_id=approval_id,
            run_id=run_id,
            provider_created_at=provider_created_at,
            cleanup_at=cleanup_at,
            state=CleanupState.ACTIVE.value,
            created_at=datetime.now(UTC).isoformat(),
            resolution=resolution,
        )

        with self._database.session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            session.expunge(record)

        return record

    def mark_active(
        self,
        record_id: str,
        provider_created_at: str | None,
        reason_sent: str,
    ) -> None:
        self._update(
            record_id,
            state=CleanupState.ACTIVE.value,
            provider_created_at=provider_created_at,
            reason_sent=reason_sent,
        )

    def resolve(
        self,
        record_id: str,
        state: CleanupState,
        resolution: str,
    ) -> None:
        self._update(
            record_id,
            state=state.value,
            resolution=resolution,
            resolved_at=datetime.now(UTC).isoformat(),
        )

    def _update(self, record_id: str, **fields: Any) -> None:
        with self._database.session() as session:
            record = session.get(PricingCleanupRecord, record_id)

            if record is None:
                return

            for key, value in fields.items():
                setattr(record, key, value)

            session.commit()

    def get(self, record_id: str) -> PricingCleanupRecord | None:
        with self._database.session() as session:
            record = session.get(PricingCleanupRecord, record_id)

            if record is not None:
                session.expunge(record)

            return record

    def due(self, now: datetime | None = None) -> list[PricingCleanupRecord]:
        """Active rows whose cleanup_at has arrived, oldest first."""
        moment = (now or datetime.now(UTC)).isoformat()

        with self._database.session() as session:
            rows = list(
                session.scalars(
                    select(PricingCleanupRecord)
                    .where(PricingCleanupRecord.state == CleanupState.ACTIVE.value)
                    .where(PricingCleanupRecord.cleanup_at <= moment)
                    .order_by(PricingCleanupRecord.cleanup_at)
                )
            )

            for row in rows:
                session.expunge(row)

            return rows

    def overdue(
        self,
        now: datetime | None = None,
        grace_hours: int = 2,
    ) -> list[PricingCleanupRecord]:
        """Rows whose cleanup is late.

        Surfaced whether or not the runner executed, because "cleanup did not
        happen" must be visible without depending on the thing that failed.
        """
        cutoff = (
            (now or datetime.now(UTC)) - timedelta(hours=grace_hours)
        ).isoformat()

        with self._database.session() as session:
            rows = list(
                session.scalars(
                    select(PricingCleanupRecord)
                    .where(PricingCleanupRecord.state == CleanupState.ACTIVE.value)
                    .where(PricingCleanupRecord.cleanup_at <= cutoff)
                    .order_by(PricingCleanupRecord.cleanup_at)
                )
            )

            for row in rows:
                session.expunge(row)

            return rows

    def open_records(self) -> list[PricingCleanupRecord]:
        """Everything not yet in a terminal state, plus anything needing eyes."""
        wanted = [
            CleanupState.PENDING_WRITE.value,
            CleanupState.ACTIVE.value,
            CleanupState.NEEDS_REVIEW.value,
            CleanupState.UNKNOWN_CLEANUP_STATE.value,
        ]

        with self._database.session() as session:
            rows = list(
                session.scalars(
                    select(PricingCleanupRecord)
                    .where(PricingCleanupRecord.state.in_(wanted))
                    .order_by(PricingCleanupRecord.cleanup_at)
                )
            )

            for row in rows:
                session.expunge(row)

            return rows


def to_payload(record: PricingCleanupRecord) -> dict[str, Any]:
    """Console projection. Carries no credential and no provider internals."""
    return {
        "id": record.id,
        "listing_id": record.listing_id,
        "stay_date": record.stay_date,
        "old_price": record.old_price,
        "new_price": record.new_price,
        "currency": record.currency,
        "state": record.state,
        "adopted": record.adopted,
        "approval_id": record.approval_id,
        "created_at": record.created_at,
        "cleanup_at": record.cleanup_at,
        "resolved_at": record.resolved_at,
        "resolution": record.resolution,
    }
