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

from sqlalchemy import select, update

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

#: How long a claim is held before it is considered stale.
#:
#: **This is a threshold for calling something abandoned, not a licence to take
#: it over.** An expired claim is reconciled to NEEDS_REVIEW and sends nothing;
#: it is never automatically reclaimed and deleted. See `CleanupState.CLAIMED`
#: for why elapsed time cannot be permission.
#:
#: Fifteen minutes is generous against the work it covers -- one provider read,
#: a comparison, at most one DELETE, each bounded by
#: `REQUEST_TIMEOUT_SECONDS = 10` -- so a live owner is very unlikely to have
#: its claim declared stale. That matters only because a false positive costs a
#: person a look, not because the margin is load-bearing for safety.
#: Lengthening it would not make anything safer.
CLAIM_LEASE_SECONDS = 15 * 60


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
    #: Claimed by one process, which is working on it now.
    #:
    #: Not terminal, but a lapsed lease does **not** return the row to the
    #: queue -- it sends the row to NEEDS_REVIEW. An expired claim is an
    #: unknown execution boundary, not an abandoned one: the process that held
    #: it may have died before its DELETE, may be frozen immediately before it,
    #: may have sent it and died before recording it, or may still resume. The
    #: provider DELETE cannot participate in our database transaction, so no
    #: amount of elapsed time distinguishes those cases.
    #:
    #: A stranded AgentGuard override costs a person a few minutes. Deleting a
    #: pricing decision a person made in the meantime is unrecoverable. So this
    #: fails closed and asks.
    CLAIMED = "CLAIMED"
    #: The fencing boundary in front of the provider DELETE.
    #:
    #: Committed by the claim holder *immediately before* it calls PriceLabs,
    #: and only if it still holds its claim. That is what stops the other
    #: hazard a token alone cannot reach: a process that proved ownership, then
    #: froze past its lease, then resumed. The token would stop its database
    #: verdict landing -- but nothing would have stopped the DELETE itself,
    #: against a row a person may already be acting on.
    #:
    #: A row left here is an **ambiguous external-write boundary**: the DELETE
    #: may have been sent or not, and nothing in either system can settle it.
    #: So automation never touches it again -- not `due`, not `expired_claims`,
    #: not reconciliation. It waits for a person, and is surfaced by
    #: `open_records` and `overdue` so the wait is visible.
    DELETE_STARTED = "DELETE_STARTED"
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

    # Mandatory for anything this system wrote. Without it only three of the
    # four checks could run, and a row that reached ACTIVE without one would be
    # quietly held to a weaker standard than every other row.
    if not record.adopted and not record.provider_created_at:
        return OwnershipCheck(
            False,
            "the record has no provider creation time to verify against",
        )

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

    def record_reason_sent(self, record_id: str, reason_sent: str) -> bool:
        """Record what we are *about* to send, before we send it.

        **This is evidence of intent, not of acceptance.** It says only that
        AgentGuard composed this exact `reason` and was about to POST it. It
        says nothing about whether the provider received or stored it, and it
        must never be read as though it did.

        Why it exists: `reason_sent` used to be written only by `mark_active`,
        which runs after a successful confirming re-read. A write whose
        re-read failed therefore left a row with an override possibly live at
        the provider and **no local record of what we had sent** -- so a
        person investigating later had nothing to compare against. That is
        exactly what happened to the 2026-09-08 row in the incident of
        2026-09-07 (see docs/PRICING_CLEANUP_V2.md).

        Restricted to `PENDING_WRITE` by the `WHERE` clause: this is the only
        window in which the value is genuinely unsent, and a later caller must
        not be able to rewrite the recorded intent of a settled row.

        It grants nothing. `provider_created_at` is still unset, the row is
        still not ACTIVE, and ownership still requires all four checks.
        """
        with self._database.session() as session:
            result = session.execute(
                update(PricingCleanupRecord)
                .where(PricingCleanupRecord.id == record_id)
                # Intent is recordable only while the write has not happened.
                .where(
                    PricingCleanupRecord.state == CleanupState.PENDING_WRITE.value
                )
                .values(reason_sent=reason_sent)
            )

            session.commit()

            return result.rowcount == 1

    def mark_active(
        self,
        record_id: str,
        provider_created_at: str | None,
        reason_sent: str,
        provider_updated_at: str | None = None,
    ) -> CleanupState:
        """Confirm a written override, or refuse to call it ACTIVE.

        **This is the only door into ACTIVE, and it is where the confirming
        checks are enforced** -- not in the caller. A row reaches ACTIVE only
        when the provider's own re-read supplies a creation time and shows the
        override untouched since. Anything else resolves to NEEDS_REVIEW with
        the override still in place, and no cleanup is ever attempted for it
        automatically.

        `provider_created_at` is mandatory for anything this system wrote. A
        row without one could only ever satisfy three of the four ownership
        checks, so admitting it to ACTIVE would create a record held to a
        weaker standard than its neighbours -- and nothing downstream would
        say so.

        `updated_at != created_at` on a freshly written override means the
        POST landed on a row that already existed rather than creating one.
        That row is somebody else's, or an earlier one of ours; either way its
        provenance is not what this record claims, so it is not cleanable on
        this record's authority.

        Returns the state actually reached, so a caller cannot assume success.
        """
        record = self.get(record_id)

        owned_by_us = record is not None and not record.adopted

        if owned_by_us and not provider_created_at:
            self.resolve(
                record_id,
                CleanupState.NEEDS_REVIEW,
                (
                    "the confirming re-read returned no creation time, so this "
                    "override cannot be verified as ours later. It is still in "
                    "place and needs a person."
                ),
            )

            return CleanupState.NEEDS_REVIEW

        if (
            owned_by_us
            and provider_updated_at is not None
            and provider_updated_at != provider_created_at
        ):
            self.resolve(
                record_id,
                CleanupState.NEEDS_REVIEW,
                (
                    "the override was already present and was modified rather "
                    "than created, so it cannot be verified as ours later. It "
                    "is in place and needs a person."
                ),
            )

            return CleanupState.NEEDS_REVIEW

        self._update(
            record_id,
            state=CleanupState.ACTIVE.value,
            provider_created_at=provider_created_at,
            reason_sent=reason_sent,
        )

        return CleanupState.ACTIVE

    def claim(
        self,
        record_id: str,
        token: str,
        now: datetime | None = None,
        lease_seconds: int = CLAIM_LEASE_SECONDS,
    ) -> bool:
        """Take exclusive ownership of one due row. True if we got it.

        **This is the mutual exclusion, and it is one atomic UPDATE.** The
        `WHERE` clause is the compare; the `SET` is the swap. Two processes
        issuing it concurrently both reach the database and exactly one matches
        a row -- the loser's `rowcount` is 0 and it does nothing. No Python
        lock, no module global, no assumption that one instance exists.

        **Only an ACTIVE row is claimable.** A CLAIMED row is never taken over,
        however old its lease: elapsed time cannot tell a dead process from a
        frozen one, and taking over could mean a second automatic DELETE
        against a date somebody has since re-pinned. Stale claims go to
        `expired_claims` and reconciliation, which delete nothing.
        """
        moment = now or datetime.now(UTC)

        stamp = moment.isoformat()

        expires = (moment + timedelta(seconds=lease_seconds)).isoformat()

        with self._database.session() as session:
            result = session.execute(
                update(PricingCleanupRecord)
                .where(PricingCleanupRecord.id == record_id)
                .where(PricingCleanupRecord.state == CleanupState.ACTIVE.value)
                .values(
                    state=CleanupState.CLAIMED.value,
                    claim_token=token,
                    claimed_at=stamp,
                    lease_until=expires,
                )
            )

            session.commit()

            return result.rowcount == 1

    def begin_delete(
        self,
        record_id: str,
        token: str,
    ) -> bool:
        """Cross the boundary into DELETE_STARTED. True if we may now call out.

        The last thing that happens before a provider DELETE, and the second
        compare-and-swap in the lifecycle: `state='CLAIMED' AND claim_token=?`.

        The claim alone is not enough to authorise the call. A process can
        claim a row, prove ownership, then stall past its lease while another
        process reconciles the row to NEEDS_REVIEW and a person starts acting
        on it. If the stalled process then resumed straight into
        `remove_override`, it would delete an override somebody was already
        handling. Its token would stop the *verdict* landing, but the provider
        call would already have happened.

        So the durable transition is taken first, and the call is made only if
        it committed. Reconciliation clears `claim_token`, so a row it settled
        fails this CAS on both predicates and nothing is sent.
        """
        with self._database.session() as session:
            result = session.execute(
                update(PricingCleanupRecord)
                .where(PricingCleanupRecord.id == record_id)
                .where(PricingCleanupRecord.state == CleanupState.CLAIMED.value)
                .where(PricingCleanupRecord.claim_token == token)
                .values(state=CleanupState.DELETE_STARTED.value)
            )

            session.commit()

            return result.rowcount == 1

    def expired_claims(
        self,
        now: datetime | None = None,
    ) -> list[PricingCleanupRecord]:
        """Claims nobody released within their lease. **Not a work queue.**

        Every row here is an unknown execution boundary. It is handed to
        reconciliation, which resolves it to NEEDS_REVIEW and sends nothing --
        never back into the claiming path, because a second automatic DELETE is
        the one outcome that could destroy a person's own pricing decision.
        """
        moment = (now or datetime.now(UTC)).isoformat()

        with self._database.session() as session:
            rows = list(
                session.scalars(
                    select(PricingCleanupRecord)
                    .where(PricingCleanupRecord.state == CleanupState.CLAIMED.value)
                    .where(PricingCleanupRecord.lease_until <= moment)
                    .order_by(PricingCleanupRecord.lease_until)
                )
            )

            for row in rows:
                session.expunge(row)

            return rows

    def release(self, record_id: str, token: str) -> bool:
        """Hand a claimed row back to the queue, unresolved.

        For the paths that end a pass without deciding anything and without
        sending anything -- the provider could not be read, the kill switch is
        off. The obligation is unchanged and no DELETE was attempted, so the
        next pass should pick it up at once rather than wait out a lease.

        This is the live owner putting the row down deliberately. It is not the
        same thing as a lease expiring, which is why the two have different
        outcomes: here we know nothing was sent.

        Conditional on still holding the claim.
        """
        with self._database.session() as session:
            result = session.execute(
                update(PricingCleanupRecord)
                .where(PricingCleanupRecord.id == record_id)
                .where(PricingCleanupRecord.claim_token == token)
                .values(
                    state=CleanupState.ACTIVE.value,
                    claim_token=None,
                    claimed_at=None,
                    lease_until=None,
                )
            )

            session.commit()

            return result.rowcount == 1

    def resolve_unclaimed(
        self,
        record_id: str,
        state: CleanupState,
        resolution: str,
    ) -> bool:
        """Settle a CLAIMED row that carries no claim token.

        `CLAIMED` is supposed to imply a token: `claim` is the only writer of
        that state and always sets one. A row without one is an invariant
        violation, and the safe thing is still to get it in front of a person
        rather than leave it stuck.

        This is deliberately **not** the unconditional branch of `resolve`. It
        is its own compare-and-swap -- `state='CLAIMED' AND claim_token IS
        NULL` -- so a malformed row can be settled without opening a path by
        which any row could bypass token ownership. A row that has since been
        claimed properly, or already settled, does not match and is left alone.
        """
        with self._database.session() as session:
            result = session.execute(
                update(PricingCleanupRecord)
                .where(PricingCleanupRecord.id == record_id)
                .where(PricingCleanupRecord.state == CleanupState.CLAIMED.value)
                .where(PricingCleanupRecord.claim_token.is_(None))
                .values(
                    state=state.value,
                    resolution=resolution,
                    resolved_at=datetime.now(UTC).isoformat(),
                    claim_token=None,
                    lease_until=None,
                )
            )

            session.commit()

            return result.rowcount == 1

    def resolve(
        self,
        record_id: str,
        state: CleanupState,
        resolution: str,
        expected_token: str | None = None,
    ) -> bool:
        """Settle a row. False when the claim was lost and nothing was written.

        `expected_token` guards the lease-expiry hazard: a slow process whose
        claim has since been reconciled must not overwrite that verdict with
        its own stale one. Given a token, the write is conditional on still
        holding the claim.
        """
        fields = {
            "state": state.value,
            "resolution": resolution,
            "resolved_at": datetime.now(UTC).isoformat(),
            # A settled row holds no claim. Leaving one would make a terminal
            # row look like work in progress to anyone reading the table.
            "claim_token": None,
            "lease_until": None,
        }

        if expected_token is None:
            self._update(record_id, **fields)

            return True

        with self._database.session() as session:
            result = session.execute(
                update(PricingCleanupRecord)
                .where(PricingCleanupRecord.id == record_id)
                .where(PricingCleanupRecord.claim_token == expected_token)
                .values(**fields)
            )

            session.commit()

            return result.rowcount == 1

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
        """Rows a caller may *attempt*, oldest first. Not rows it may delete.

        Selecting is not claiming: two processes calling this concurrently get
        the same list, which is exactly why `claim` exists and why the runner
        claims before it reads the provider. This is the candidate set; `claim`
        is the gate.

        ACTIVE rows only. A CLAIMED row is never offered here, whatever its
        lease says -- `expired_claims` is where those go, and they go to a
        person rather than back into the automatic path.
        """
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
                    .where(
                        PricingCleanupRecord.state.in_(
                            (
                                CleanupState.ACTIVE.value,
                                # A row stuck under a claim is *more* overdue,
                                # not less. Hiding it here would make a crashed
                                # process invisible.
                                CleanupState.CLAIMED.value,
                                # Likewise a row abandoned at the delete
                                # boundary. Automation will never resolve it,
                                # so surfacing it is the only way it is ever
                                # seen.
                                CleanupState.DELETE_STARTED.value,
                            )
                        )
                    )
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
            CleanupState.CLAIMED.value,
            CleanupState.DELETE_STARTED.value,
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

    def workload(self, now: datetime | None = None) -> dict[str, Any]:
        """What cleanup currently owes, for a person about to run a pass.

        Read-only and deliberately inert: it counts rows and changes none of
        them. Nothing here claims, reconciles, ages a lease out, or moves a
        row between states -- an operator looking at the queue must not be a
        way of altering it.

        `due_now` is the same ACTIVE-and-arrived predicate `due` selects on, so
        the number shown before a manual run is the work that run will attempt.
        It is not a promise: `due` is the candidate set, and `claim` is still
        the gate.

        `needs_attention` carries the rows automation will never resolve --
        `DELETE_STARTED` most of all, which is hands-off by design. A count
        alone would say a person is needed without saying which night, so the
        rows come with it.
        """
        moment = (now or datetime.now(UTC)).isoformat()

        counts = {state.value: 0 for state in CleanupState}
        due_now = 0
        oldest_overdue: str | None = None

        # One pass over the open rows: this queue is small by construction
        # (one row per temporary override, all of them expiring within
        # MAX_LIFETIME_DAYS), so a group-by would buy nothing and would still
        # need a second query for the rows themselves.
        records = self.open_records()

        for record in records:
            counts[record.state] = counts.get(record.state, 0) + 1

            arrived = record.cleanup_at <= moment

            if record.state == CleanupState.ACTIVE.value and arrived:
                due_now += 1

            # A claimed or abandoned row is *more* overdue, not less, so the
            # age is measured over every state automation could be stuck in.
            stuck = arrived and record.state in _OVERDUE_STATES

            if stuck and (
                oldest_overdue is None or record.cleanup_at < oldest_overdue
            ):
                oldest_overdue = record.cleanup_at

        return {
            "counted_at": moment,
            "pending_write": counts[CleanupState.PENDING_WRITE.value],
            "active": counts[CleanupState.ACTIVE.value],
            "due_now": due_now,
            "claimed": counts[CleanupState.CLAIMED.value],
            "delete_started": counts[CleanupState.DELETE_STARTED.value],
            "needs_review": counts[CleanupState.NEEDS_REVIEW.value],
            "unknown_cleanup_state": counts[
                CleanupState.UNKNOWN_CLEANUP_STATE.value
            ],
            "oldest_overdue_at": oldest_overdue,
            "oldest_overdue_hours": _age_hours(oldest_overdue, moment),
            "needs_attention": [
                to_payload(record)
                for record in records
                if record.state in _NEEDS_A_PERSON
            ],
        }


#: States whose age counts as cleanup running late. `PENDING_WRITE` is absent
#: on purpose: it is the moment between the row and the provider call, not a
#: cleanup that failed to happen.
_OVERDUE_STATES: frozenset[str] = frozenset(
    {
        CleanupState.ACTIVE.value,
        CleanupState.CLAIMED.value,
        CleanupState.DELETE_STARTED.value,
    }
)

#: Rows automation will never resolve on its own.
_NEEDS_A_PERSON: frozenset[str] = frozenset(
    {
        CleanupState.DELETE_STARTED.value,
        CleanupState.NEEDS_REVIEW.value,
        CleanupState.UNKNOWN_CLEANUP_STATE.value,
    }
)


def _age_hours(stamp: str | None, moment: str) -> float | None:
    """Hours between two ISO stamps, or None when there is nothing to age.

    Unknown stays unknown: an unparsable timestamp returns None rather than
    zero, which would read as "nothing is overdue".
    """
    if stamp is None:
        return None

    try:
        then = datetime.fromisoformat(stamp)
        now = datetime.fromisoformat(moment)

    except ValueError:
        return None

    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    return round((now - then).total_seconds() / 3600.0, 2)


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
