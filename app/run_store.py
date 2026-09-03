"""Durable run state: the Run aggregate and its execution steps."""

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.database import Database, get_database
from app.db_models import RunRecord, RunStepRecord


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ALLOWED_STATUSES: tuple[str, ...] = tuple(status.value for status in RunStatus)

DEFAULT_LIMIT = 20

MIN_LIMIT = 1

MAX_LIMIT = 100


def validate_status(status: str | None) -> str | None:
    """Return the status unchanged, or raise if it is not an allowed value."""
    if status is None:
        return None

    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"Unsupported run status: {status!r}. "
            f"Allowed values: {', '.join(ALLOWED_STATUSES)}."
        )

    return status


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


class StepType(str, Enum):
    MODEL_RESPONSE = "MODEL_RESPONSE"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    TOOL_FAILED = "TOOL_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    RUN_RECONCILED = "RUN_RECONCILED"


def now() -> str:
    return datetime.now(UTC).isoformat()


def dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def run_to_dict(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "status": record.status,
        "requested_by_user_id": record.requested_by_user_id,
        "user_message": record.user_message,
        "final_answer": record.final_answer,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def step_to_dict(record: RunStepRecord) -> dict[str, Any]:
    return {
        "step_number": record.step_number,
        "step_type": record.step_type,
        "tool_name": record.tool_name,
        "arguments": load(record.arguments_json),
        "result": load(record.result_json),
        "error": load(record.error_json),
        "created_at": record.created_at,
    }


class RunStore:
    def __init__(
        self,
        database: Database | None = None,
    ) -> None:
        self._database = database or get_database()

    def create_run(
        self,
        user_message: str,
        requested_by_user_id: str | None = None,
    ) -> str:
        """Start a run in RUNNING and return its id."""
        timestamp = now()

        record = RunRecord(
            run_id=str(uuid4()),
            status=RunStatus.RUNNING.value,
            requested_by_user_id=requested_by_user_id,
            user_message=user_message,
            final_answer=None,
            conversation_json="[]",
            created_at=timestamp,
            updated_at=timestamp,
        )

        with self._database.session() as session:
            session.add(record)
            session.commit()

            return record.run_id

    def get_run(
        self,
        run_id: str,
    ) -> RunRecord | None:
        with self._database.session() as session:
            return session.get(RunRecord, run_id)

    def list_runs(
        self,
        status: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Newest first, optionally scoped to one status.

        Raises:
            ValueError: If status is not an allowed value.
        """
        validated_status = validate_status(status)

        statement = select(RunRecord)

        if validated_status is not None:
            statement = statement.where(RunRecord.status == validated_status)

        statement = statement.order_by(RunRecord.created_at.desc()).limit(limit)

        with self._database.session() as session:
            return [run_to_dict(record) for record in session.scalars(statement)]

    def status_counts(self) -> dict[str, int]:
        statement = select(RunRecord.status, func.count(RunRecord.run_id)).group_by(
            RunRecord.status
        )

        with self._database.session() as session:
            return dict(session.execute(statement).all())

    def count_created_on(self, day: str) -> int:
        """Runs created on an ISO date (YYYY-MM-DD), matched on the ISO prefix."""
        statement = select(func.count(RunRecord.run_id)).where(
            RunRecord.created_at.startswith(day)
        )

        with self._database.session() as session:
            return session.scalar(statement) or 0

    def list_stale_running(
        self,
        cutoff: str,
    ) -> list[str]:
        """Run IDs still RUNNING whose updated_at is older than an ISO cutoff.

        Only RUNNING is considered. A run parked in WAITING_FOR_APPROVAL is
        waiting on a human, not stalled, however long it has been.
        """
        statement = (
            select(RunRecord.run_id)
            .where(RunRecord.status == RunStatus.RUNNING.value)
            .where(RunRecord.updated_at < cutoff)
            .order_by(RunRecord.updated_at)
        )

        with self._database.session() as session:
            return list(session.scalars(statement))

    def save_conversation(
        self,
        run_id: str,
        conversation: list[dict[str, Any]],
    ) -> None:
        """Persist resumable state. Always JSON -- never a provider SDK object."""
        self.apply(
            run_id,
            conversation_json=json.dumps(conversation),
        )

    def await_approval(
        self,
        run_id: str,
        conversation: list[dict[str, Any]],
    ) -> None:
        self.apply(
            run_id,
            status=RunStatus.WAITING_FOR_APPROVAL.value,
            conversation_json=json.dumps(conversation),
        )

    def resume(
        self,
        run_id: str,
    ) -> None:
        self.apply(run_id, status=RunStatus.RUNNING.value)

    def complete(
        self,
        run_id: str,
        final_answer: str,
    ) -> None:
        self.apply(
            run_id,
            status=RunStatus.COMPLETED.value,
            final_answer=final_answer,
        )

    def fail(
        self,
        run_id: str,
        final_answer: str | None = None,
    ) -> None:
        self.apply(
            run_id,
            status=RunStatus.FAILED.value,
            final_answer=final_answer,
        )

    def cancel(
        self,
        run_id: str,
        final_answer: str | None = None,
    ) -> None:
        self.apply(
            run_id,
            status=RunStatus.CANCELLED.value,
            final_answer=final_answer,
        )

    def apply(
        self,
        run_id: str,
        **changes: Any,
    ) -> None:
        """Shared writer for the domain operations above. Not a public setter."""
        with self._database.session() as session:
            record = session.get(RunRecord, run_id)

            if record is None:
                raise ValueError(f"Unknown run ID: {run_id}")

            for field_name, value in changes.items():
                setattr(record, field_name, value)

            record.updated_at = now()

            session.commit()

    # -- steps -------------------------------------------------------------

    def add_step(
        self,
        run_id: str,
        step_type: StepType,
        tool_name: str | None = None,
        arguments: Any = None,
        result: Any = None,
        error: Any = None,
    ) -> int:
        with self._database.session() as session:
            used = session.scalars(
                select(RunStepRecord.step_number).where(RunStepRecord.run_id == run_id)
            ).all()

            step_number = (max(used) if used else 0) + 1

            session.add(
                RunStepRecord(
                    run_id=run_id,
                    step_number=step_number,
                    step_type=step_type.value,
                    tool_name=tool_name,
                    arguments_json=dump(arguments),
                    result_json=dump(result),
                    error_json=dump(error),
                    created_at=now(),
                )
            )

            session.commit()

            return step_number

    def list_steps(
        self,
        run_id: str,
    ) -> list[dict[str, Any]]:
        with self._database.session() as session:
            statement = (
                select(RunStepRecord)
                .where(RunStepRecord.run_id == run_id)
                .order_by(RunStepRecord.step_number)
            )

            return [step_to_dict(record) for record in session.scalars(statement)]
