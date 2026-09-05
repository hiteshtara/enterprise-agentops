"""Executes owed cleanups. One DELETE per record, or nothing.

Cleanup runs without a fresh human approval, and that is a deliberate position
rather than an omission. It is *restorative*: it returns a date to the state it
had before a change a person already approved, within limits that person set.
Requiring a new approval per cleanup would mean an unapproved cleanup leaves a
permanent pin -- the failure mode inverted.

What makes that safe is how narrow it is. Cleanup only ever removes, never sets
a price. It only touches a date named in a row AgentGuard wrote itself. It
refuses unless ownership is proven against the recorded marker, price and
timestamps. And it cannot discover work for itself: the row bounds it.

It still sits behind both kill switches, and every action is audited with the
`approval_id` of the human decision that created the obligation.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.connectors.pricelabs.write_client import (
    PriceLabsWriteClient,
    PricingWritesDisabled,
    WriteOutcome,
)
from app.db_models import PricingCleanupRecord
from app.pricing_cleanup import (
    CleanupState,
    PricingCleanupStore,
    check_ownership,
)
from app.pricing_config import bands_for

#: Reported instead of a terminal state when this process's verdict did not
#: land, because its claim was gone by the time it tried to write. Not a
#: `CleanupState`: the row never entered it, and inventing a state for
#: "we do not know what the row says" would put that fiction in the database.
OWNERSHIP_LOST = "OWNERSHIP_LOST"


@dataclass(frozen=True)
class CleanupOutcome:
    record_id: str
    listing_id: str
    stay_date: str
    #: What this pass *concluded*. Only the durable state when `committed`.
    state: CleanupState
    detail: str
    #: Whether a provider DELETE was attempted. True even when the verdict was
    #: not committed -- the attempt happened and must stay visible.
    deleted: bool = False
    #: False when the durable write was refused because the claim was lost.
    committed: bool = True

    @property
    def reported_state(self) -> str:
        """What may honestly be said about the row after this pass.

        An uncommitted verdict reports `OWNERSHIP_LOST` rather than the state
        it wanted, because the row's actual state was decided by whoever holds
        the claim now and this process has not read it back.
        """
        return self.state.value if self.committed else OWNERSHIP_LOST


class PricingCleanupRunner:
    """Walks the due queue once. Never loops, never retries."""

    def __init__(
        self,
        store: PricingCleanupStore,
        reader,
        writer: PriceLabsWriteClient,
        audit=None,
    ) -> None:
        self._store = store
        self._reader = reader
        self._writer = writer
        self._audit = audit

    def _record_audit(self, event: str, record: PricingCleanupRecord, **extra: Any):
        if self._audit is None:
            return

        self._audit.record(
            event,
            {
                "cleanup_id": record.id,
                "listing_id": record.listing_id,
                "stay_date": record.stay_date,
                # Ties every cleanup back to the human decision that created
                # the obligation, even though no one approved this run.
                "approval_id": record.approval_id,
                **extra,
            },
            run_id=record.run_id,
        )

    def _override_for(self, record: PricingCleanupRecord) -> dict[str, Any] | None:
        for row in self._reader.overrides(record.listing_id, record.pms):
            if row.get("date") == record.stay_date:
                return row

        return None

    def run_once(self, now: datetime | None = None) -> list[CleanupOutcome]:
        """Process every due record this process manages to claim.

        Two handles drive this runner -- the operator route and the hourly job
        -- and they can run at the same time, in different processes, on
        different hosts. `due()` hands both the same candidate list, so
        selecting a row is not permission to act on it. Each must be *claimed*
        first, by a compare-and-swap in the database, and a row this process
        did not claim is skipped without a single provider call.

        The claim comes before the provider read, not before the DELETE. That
        ordering is what closes the hole: a loser never even reads, so two
        processes can never hold an ownership proof for the same override.

        A pass also reconciles claims whose lease expired. Those are **not**
        taken over and retried -- they are resolved to NEEDS_REVIEW without a
        single write. See `_reconcile`.
        """
        outcomes: list[CleanupOutcome] = [
            self._reconcile(record) for record in self._store.expired_claims(now=now)
        ]

        for record in self._store.due(now=now):
            token = str(uuid.uuid4())

            if not self._store.claim(record.id, token, now=now):
                # Someone else owns this row right now. Not an error and not
                # something to wait for: their pass will resolve it.
                continue

            outcomes.append(self._process(record, token))

        return outcomes

    def _reconcile(self, record: PricingCleanupRecord) -> CleanupOutcome:
        """Settle a claim nobody released. Reads at most; deletes never.

        An expired claim is an *unknown execution boundary*. The process that
        held it may have died before its DELETE, may be frozen a line before
        it, may have sent it and died before recording the result, or may still
        resume. The PriceLabs DELETE cannot join our database transaction, so
        no amount of elapsed time tells those apart -- a lease can bound a
        window, but it can never license a second automatic DELETE.

        So this fails closed: NEEDS_REVIEW, zero writes. The provider is read
        only to describe what a person will find, and a read that fails is
        itself just something to report.

        The asymmetry is the whole argument. A stranded AgentGuard override
        costs someone a few minutes with the queue. Deleting a pricing decision
        a person made in the meantime cannot be undone.
        """
        try:
            override = self._override_for(record)

            found = (
                "the override is no longer at PriceLabs"
                if override is None
                else f"an override for {override.get('price')} is still in place"
            )

        except PriceLabsUnavailable:
            found = "PriceLabs could not be read"

        detail = (
            "a cleanup claim expired without being released, so it is "
            f"unknown whether its removal was sent. On checking, {found}. "
            "Nothing was deleted: an expired claim is not permission to "
            "try again. This needs a person."
        )

        if record.claim_token is not None:
            return self._resolve(
                record,
                record.claim_token,
                CleanupState.NEEDS_REVIEW,
                detail,
            )

        # CLAIMED with no token: `claim` is the only writer of that state and
        # always sets one, so this should be unreachable. Settle it through its
        # own compare-and-swap rather than an unconditional write -- a
        # malformed row must not become a way for any row to bypass token
        # ownership -- and say plainly that an invariant broke.
        invariant = f"{detail} It also reached CLAIMED with no claim token, which should be impossible."

        committed = self._store.resolve_unclaimed(
            record.id,
            CleanupState.NEEDS_REVIEW,
            invariant,
        )

        self._record_audit(
            "PRICING_CLEANUP" if committed else "PRICING_CLEANUP_STALE_OWNER",
            record,
            state=CleanupState.NEEDS_REVIEW.value,
            attempted_state=CleanupState.NEEDS_REVIEW.value,
            provider_delete_attempted=False,
            detail=invariant,
            invariant_violation="CLAIMED row carried no claim token",
        )

        return CleanupOutcome(
            record.id,
            record.listing_id,
            record.stay_date,
            CleanupState.NEEDS_REVIEW,
            invariant,
            committed=committed,
        )

    def _process(
        self,
        record: PricingCleanupRecord,
        token: str,
    ) -> CleanupOutcome:
        bands = bands_for(record.listing_id)

        if bands is None:
            return self._resolve(
                record,
                token,
                CleanupState.NEEDS_REVIEW,
                "this listing has no owner-approved bands",
            )

        try:
            override = self._override_for(record)

        except PriceLabsUnavailable:
            # Not resolved: nothing was sent, so the row goes straight back to
            # ACTIVE and the next pass retries the *read*. This is the live
            # owner putting it down, not a lease lapsing -- we know no DELETE
            # was attempted, which is why it may re-enter the automatic path.
            self._store.release(record.id, token)

            return CleanupOutcome(
                record.id,
                record.listing_id,
                record.stay_date,
                CleanupState.ACTIVE,
                "PriceLabs could not be read; nothing was sent",
            )

        if override is None:
            return self._resolve(
                record,
                token,
                CleanupState.VANISHED,
                (
                    "the override was already gone; the date is back on "
                    "dynamic pricing"
                ),
            )

        ownership = check_ownership(record, override)

        if ownership.refused:
            return self._resolve(
                record,
                token,
                CleanupState.NEEDS_REVIEW,
                f"not removed: {ownership.reason}",
            )

        try:
            result = self._writer.remove_override(
                record.listing_id,
                record.pms,
                record.stay_date,
                automation_enabled=bands.automation_enabled,
            )

        except PricingWritesDisabled as exc:
            self._store.release(record.id, token)

            return CleanupOutcome(
                record.id,
                record.listing_id,
                record.stay_date,
                CleanupState.ACTIVE,
                str(exc),
            )

        except PriceLabsUnavailable:
            return self._resolve(
                record,
                token,
                CleanupState.UNKNOWN_CLEANUP_STATE,
                (
                    "PriceLabs did not answer the removal. It may already be "
                    "gone. Check before doing anything else — this is not "
                    "retried."
                ),
                deleted=True,
            )

        if result.outcome is WriteOutcome.CONFIRMED_APPLIED:
            return self._resolve(
                record,
                token,
                CleanupState.CLEANED_UP,
                result.message,
                deleted=True,
            )

        if result.outcome is WriteOutcome.UNKNOWN_WRITE_STATE:
            return self._resolve(
                record,
                token,
                CleanupState.UNKNOWN_CLEANUP_STATE,
                result.message,
                deleted=True,
            )

        return self._resolve(
            record,
            token,
            CleanupState.NEEDS_REVIEW,
            f"the removal did not take effect: {result.message}",
            deleted=True,
        )

    def _resolve(
        self,
        record: PricingCleanupRecord,
        token: str | None,
        state: CleanupState,
        detail: str,
        deleted: bool = False,
    ) -> CleanupOutcome:
        """Commit this pass's verdict, and report only what actually landed.

        The write is conditional on still holding the claim, so a process whose
        lease lapsed cannot overwrite the verdict reconciliation already
        recorded. When that refusal happens the audit must say so: an event
        announcing `CLEANED_UP` for a row the database never moved would make
        the trail disagree with the thing it exists to describe.

        So a refused write emits `PRICING_CLEANUP_STALE_OWNER` instead --
        carrying what was attempted, and crucially whether a provider DELETE
        was already sent, because that side effect is real whatever happened to
        our bookkeeping afterwards.
        """
        committed = self._store.resolve(
            record.id,
            state,
            detail,
            expected_token=token,
        )

        if committed:
            self._record_audit(
                "PRICING_CLEANUP",
                record,
                state=state.value,
                detail=detail,
                deleted=deleted,
            )

        else:
            self._record_audit(
                "PRICING_CLEANUP_STALE_OWNER",
                record,
                attempted_state=state.value,
                provider_delete_attempted=deleted,
                detail=detail,
                claim_token=token,
                ownership=(
                    "the claim was gone before this verdict could be written; "
                    "the row was not changed by this process"
                ),
            )

        return CleanupOutcome(
            record.id,
            record.listing_id,
            record.stay_date,
            state,
            detail,
            deleted=deleted,
            committed=committed,
        )


def summarise(outcomes: list[CleanupOutcome]) -> dict[str, Any]:
    """What the pass did, counted by what actually committed.

    `reported_state` rather than `state`, so a verdict that lost its claim is
    counted as `OWNERSHIP_LOST` and never inflates the tally of rows this pass
    genuinely settled. `deleted` still counts provider DELETE *attempts*,
    committed or not: the side effect happened either way.
    """
    counts: dict[str, int] = {}

    for outcome in outcomes:
        counts[outcome.reported_state] = counts.get(outcome.reported_state, 0) + 1

    return {
        "processed": len(outcomes),
        "deleted": sum(1 for o in outcomes if o.deleted),
        "by_state": counts,
        "ran_at": datetime.now(UTC).isoformat(),
        "records": [
            {
                "id": o.record_id,
                "listing_id": o.listing_id,
                "stay_date": o.stay_date,
                "state": o.reported_state,
                "attempted_state": o.state.value,
                "committed": o.committed,
                "detail": o.detail,
            }
            for o in outcomes
        ],
    }
