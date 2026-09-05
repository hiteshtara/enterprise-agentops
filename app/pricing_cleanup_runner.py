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


@dataclass(frozen=True)
class CleanupOutcome:
    record_id: str
    listing_id: str
    stay_date: str
    state: CleanupState
    detail: str
    deleted: bool = False


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
        """Process every due record exactly once."""
        outcomes: list[CleanupOutcome] = []

        for record in self._store.due(now=now):
            outcomes.append(self._process(record))

        return outcomes

    def _process(self, record: PricingCleanupRecord) -> CleanupOutcome:
        bands = bands_for(record.listing_id)

        if bands is None:
            return self._resolve(
                record,
                CleanupState.NEEDS_REVIEW,
                "this listing has no owner-approved bands",
            )

        try:
            override = self._override_for(record)

        except PriceLabsUnavailable:
            # Not resolved: the record stays ACTIVE and the next run retries
            # the *read*. Only a DELETE is never retried.
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
                CleanupState.CLEANED_UP,
                result.message,
                deleted=True,
            )

        if result.outcome is WriteOutcome.UNKNOWN_WRITE_STATE:
            return self._resolve(
                record,
                CleanupState.UNKNOWN_CLEANUP_STATE,
                result.message,
                deleted=True,
            )

        return self._resolve(
            record,
            CleanupState.NEEDS_REVIEW,
            f"the removal did not take effect: {result.message}",
            deleted=True,
        )

    def _resolve(
        self,
        record: PricingCleanupRecord,
        state: CleanupState,
        detail: str,
        deleted: bool = False,
    ) -> CleanupOutcome:
        self._store.resolve(record.id, state, detail)

        self._record_audit(
            "PRICING_CLEANUP",
            record,
            state=state.value,
            detail=detail,
            deleted=deleted,
        )

        return CleanupOutcome(
            record.id,
            record.listing_id,
            record.stay_date,
            state,
            detail,
            deleted=deleted,
        )


def summarise(outcomes: list[CleanupOutcome]) -> dict[str, Any]:
    counts: dict[str, int] = {}

    for outcome in outcomes:
        counts[outcome.state.value] = counts.get(outcome.state.value, 0) + 1

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
                "state": o.state.value,
                "detail": o.detail,
            }
            for o in outcomes
        ],
    }
