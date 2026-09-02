"""Manual reconciliation of runs abandoned by a crashed process.

A run is marked RUNNING while the agent loop drives it. If the process dies
mid-drive nothing ever moves it on, so it would sit RUNNING forever and show up
in the console as permanently in-flight.

This is deliberately an explicit, operator-triggered sweep -- no worker, no
scheduler, no heartbeat. V1 calls it from an admin endpoint.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.audit_store import AuditStore
from app.run_store import RunStatus, RunStore, StepType

DEFAULT_STALE_AFTER_SECONDS = 900

MIN_STALE_AFTER_SECONDS = 1

MAX_STALE_AFTER_SECONDS = 86_400

RECONCILED_ANSWER = "Run interrupted before completion"


def validate_stale_after(seconds: int) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise TypeError(
            f"stale_after_seconds must be an integer, got {type(seconds).__name__}."
        )

    if seconds < MIN_STALE_AFTER_SECONDS or seconds > MAX_STALE_AFTER_SECONDS:
        raise ValueError(
            f"stale_after_seconds must be between {MIN_STALE_AFTER_SECONDS} and "
            f"{MAX_STALE_AFTER_SECONDS}, got {seconds}."
        )

    return seconds


class ReconciliationService:
    """Transitions abandoned RUNNING runs to FAILED with a durable explanation."""

    def __init__(
        self,
        run_store: RunStore,
        audit_store: AuditStore,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        self.run_store = run_store
        self.audit_store = audit_store
        self.stale_after_seconds = validate_stale_after(stale_after_seconds)

    def cutoff(self, now: datetime | None = None) -> str:
        moment = now or datetime.now(UTC)

        return (moment - timedelta(seconds=self.stale_after_seconds)).isoformat()

    def reconcile(
        self,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Sweep once. Returns a summary of every run that was reconciled.

        Only RUNNING runs are touched. WAITING_FOR_APPROVAL, COMPLETED, FAILED
        and CANCELLED are left exactly as they are.
        """
        cutoff = self.cutoff(now)

        reconciled: list[dict[str, Any]] = []

        for run_id in self.run_store.list_stale_running(cutoff):
            record = self.run_store.get_run(run_id)

            if record is None:
                continue

            details = {
                "reason": RECONCILED_ANSWER,
                "previous_status": record.status,
                "stale_after_seconds": self.stale_after_seconds,
                "last_updated_at": record.updated_at,
            }

            self.run_store.add_step(
                run_id,
                StepType.RUN_RECONCILED,
                error=details,
            )

            self.audit_store.record(
                "RUN_RECONCILED",
                details,
                run_id=run_id,
            )

            self.run_store.fail(run_id, RECONCILED_ANSWER)

            reconciled.append(
                {
                    "run_id": run_id,
                    "previous_status": RunStatus.RUNNING.value,
                    "status": RunStatus.FAILED.value,
                    "reason": RECONCILED_ANSWER,
                }
            )

        return reconciled
