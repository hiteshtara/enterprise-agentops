"""Dashboard summary assembled from the stores.

Lives in the backend so the console renders numbers rather than deriving them,
keeping counting rules in one place. Read-only.
"""

from datetime import UTC, datetime
from typing import Any

from app.approval_store import ApprovalStatus, ApprovalStore
from app.audit_store import AuditStore
from app.run_store import RunStatus, RunStore

RECENT_RUNS = 5

RECENT_EVENTS = 10


def zeroed(keys) -> dict[str, int]:
    """Every known key present as 0, so the console never renders a gap."""
    return {key: 0 for key in keys}


class OverviewService:
    def __init__(
        self,
        run_store: RunStore,
        approval_store: ApprovalStore,
        audit_store: AuditStore,
    ) -> None:
        self.run_store = run_store
        self.approval_store = approval_store
        self.audit_store = audit_store

    def build(self, today: str | None = None) -> dict[str, Any]:
        day = today or datetime.now(UTC).date().isoformat()

        runs_by_status = zeroed(status.value for status in RunStatus)
        runs_by_status.update(self.run_store.status_counts())

        approvals_by_status = zeroed(status.value for status in ApprovalStatus)
        approvals_by_status.update(self.approval_store.count_by_status())

        events_by_type = self.audit_store.count_by_type()

        return {
            "runs_today": self.run_store.count_created_on(day),
            "runs_total": sum(runs_by_status.values()),
            "runs_by_status": runs_by_status,
            "approvals_by_status": approvals_by_status,
            "pending_approvals": approvals_by_status[ApprovalStatus.PENDING.value],
            "tool_executions": events_by_type.get("TOOL_EXECUTED", 0),
            "tool_failures": events_by_type.get("TOOL_FAILED", 0),
            "events_by_type": events_by_type,
            "recent_runs": self.run_store.list_runs(limit=RECENT_RUNS),
            "recent_events": self.audit_store.list_events(limit=RECENT_EVENTS),
        }
