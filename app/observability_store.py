"""Persistence and aggregation for execution metrics.

Kept apart from the audit trail on purpose. Audit answers "who did what, when"
and is a compliance record; these tables answer "how did it perform" and are a
measurement record. Neither is derived from the other.
"""

import json
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.approval_store import ApprovalStatus
from app.database import Database, get_database
from app.db_models import (
    ApprovalRecord,
    ModelExecutionRecord,
    RunRecord,
    ToolExecutionRecord,
)
from app.protocol import ModelUsage


class ExecutionStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def dump(value: Any) -> str | None:
    if value is None:
        return None

    try:
        return json.dumps(value)

    except (TypeError, ValueError):
        return json.dumps(str(value))


def load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class ModelExecutionStore:
    def __init__(self, database: Database | None = None) -> None:
        self._database = database or get_database()

    def record(
        self,
        run_id: str,
        provider: str,
        model: str | None,
        status: ExecutionStatus,
        started_at: str,
        completed_at: str | None,
        duration_ms: int | None,
        usage: ModelUsage | None = None,
        estimated_cost_usd: float | None = None,
        provider_request_id: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> str:
        with self._database.session() as session:
            used = session.scalars(
                select(ModelExecutionRecord.sequence).where(
                    ModelExecutionRecord.run_id == run_id
                )
            ).all()

            record = ModelExecutionRecord(
                model_execution_id=str(uuid4()),
                run_id=run_id,
                sequence=(max(used) if used else 0) + 1,
                provider=provider,
                model=model,
                provider_request_id=provider_request_id,
                status=status.value,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                cached_input_tokens=usage.cached_input_tokens if usage else None,
                reasoning_tokens=usage.reasoning_tokens if usage else None,
                estimated_cost_usd=estimated_cost_usd,
                error_type=error_type,
                error_message=error_message,
            )

            session.add(record)
            session.commit()

            return record.model_execution_id

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        statement = (
            select(ModelExecutionRecord)
            .where(ModelExecutionRecord.run_id == run_id)
            .order_by(ModelExecutionRecord.sequence)
        )

        with self._database.session() as session:
            return [
                {
                    "sequence": record.sequence,
                    "provider": record.provider,
                    "model": record.model,
                    "status": record.status,
                    "started_at": record.started_at,
                    "completed_at": record.completed_at,
                    "duration_ms": record.duration_ms,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "total_tokens": record.total_tokens,
                    "cached_input_tokens": record.cached_input_tokens,
                    "reasoning_tokens": record.reasoning_tokens,
                    "estimated_cost_usd": record.estimated_cost_usd,
                    "error_type": record.error_type,
                }
                for record in session.scalars(statement)
            ]


class ToolExecutionStore:
    def __init__(self, database: Database | None = None) -> None:
        self._database = database or get_database()

    def failures_so_far(self, run_id: str, tool_name: str) -> int:
        """How many executions of this tool already failed in this run.

        This is the retry number for the execution about to be recorded: a call
        is a retry only if the same tool already failed here.
        """
        statement = select(func.count(ToolExecutionRecord.tool_execution_id)).where(
            ToolExecutionRecord.run_id == run_id,
            ToolExecutionRecord.tool_name == tool_name,
            ToolExecutionRecord.status == ExecutionStatus.FAILED.value,
        )

        with self._database.session() as session:
            return session.scalar(statement) or 0

    def record(
        self,
        run_id: str,
        tool_name: str,
        status: ExecutionStatus,
        started_at: str,
        completed_at: str | None,
        duration_ms: int | None,
        tool_call_id: str | None = None,
        arguments: Any = None,
        result: Any = None,
        error: Any = None,
        retry_number: int | None = None,
    ) -> str:
        attempt = (
            retry_number
            if retry_number is not None
            else self.failures_so_far(run_id, tool_name)
        )

        record = ToolExecutionRecord(
            tool_execution_id=str(uuid4()),
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status=status.value,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            retry_number=attempt,
            arguments_json=dump(arguments),
            result_json=dump(result),
            error_json=dump(error),
        )

        with self._database.session() as session:
            session.add(record)
            session.commit()

            return record.tool_execution_id

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        statement = (
            select(ToolExecutionRecord)
            .where(ToolExecutionRecord.run_id == run_id)
            .order_by(
                ToolExecutionRecord.started_at, ToolExecutionRecord.tool_execution_id
            )
        )

        with self._database.session() as session:
            return [
                {
                    "tool_name": record.tool_name,
                    "status": record.status,
                    "started_at": record.started_at,
                    "completed_at": record.completed_at,
                    "duration_ms": record.duration_ms,
                    "retry_number": record.retry_number,
                    "arguments": load(record.arguments_json),
                    "error": load(record.error_json),
                }
                for record in session.scalars(statement)
            ]


def sum_or_none(values: list[int | None]) -> int | None:
    """Sum the known values, or None when nothing was reported.

    A partial report still sums: if one call reported tokens and another did
    not, the total is the known part, not a fabricated whole.
    """
    known = [value for value in values if value is not None]

    return sum(known) if known else None


class RunMetricsService:
    """Assembles the metrics for one run from measured, persisted values."""

    def __init__(
        self,
        database: Database | None = None,
    ) -> None:
        self._database = database or get_database()
        self.model_executions = ModelExecutionStore(database=self._database)
        self.tool_executions = ToolExecutionStore(database=self._database)

    def approval_wait_ms(self, run_id: str) -> int | None:
        """Total time humans spent deciding, summed over resolved approvals.

        Derived from durable approval timestamps only. An approval still
        pending contributes nothing: its wait has not finished. When no
        approval has been resolved the answer is None, not zero.
        """
        statement = select(ApprovalRecord.created_at, ApprovalRecord.resolved_at).where(
            ApprovalRecord.run_id == run_id,
            ApprovalRecord.status != ApprovalStatus.PENDING.value,
        )

        with self._database.session() as session:
            rows = session.execute(statement).all()

        waits: list[int] = []

        for created_at, resolved_at in rows:
            if not created_at or not resolved_at:
                continue

            try:
                started = datetime.fromisoformat(created_at)
                ended = datetime.fromisoformat(resolved_at)

            except ValueError:
                continue

            elapsed = round((ended - started).total_seconds() * 1000)

            if elapsed >= 0:
                waits.append(elapsed)

        return sum(waits) if waits else None

    def elapsed_ms(self, run_id: str) -> int | None:
        with self._database.session() as session:
            record = session.get(RunRecord, run_id)

        if record is None:
            return None

        try:
            started = datetime.fromisoformat(record.created_at)
            ended = datetime.fromisoformat(record.updated_at)

        except ValueError:
            return None

        elapsed = round((ended - started).total_seconds() * 1000)

        return elapsed if elapsed >= 0 else None

    def build(self, run_id: str) -> dict[str, Any]:
        models = self.model_executions.list_for_run(run_id)
        tools = self.tool_executions.list_for_run(run_id)

        costs = [
            model["estimated_cost_usd"]
            for model in models
            if model["estimated_cost_usd"] is not None
        ]

        model_ms = sum_or_none([model["duration_ms"] for model in models]) or 0
        tool_ms = sum_or_none([tool["duration_ms"] for tool in tools]) or 0

        return {
            "run_id": run_id,
            "elapsed_ms": self.elapsed_ms(run_id),
            # Measured time inside provider calls and tool callables. Excludes
            # any time the run spent waiting for a human.
            "active_execution_ms": model_ms + tool_ms,
            "approval_wait_ms": self.approval_wait_ms(run_id),
            "model_calls": len(models),
            "model_duration_ms": model_ms,
            "tool_calls": len(tools),
            "tool_duration_ms": tool_ms,
            "tool_failures": sum(
                1 for tool in tools if tool["status"] == ExecutionStatus.FAILED.value
            ),
            "tool_retries": sum(1 for tool in tools if tool["retry_number"] > 0),
            "input_tokens": sum_or_none([m["input_tokens"] for m in models]),
            "output_tokens": sum_or_none([m["output_tokens"] for m in models]),
            "total_tokens": sum_or_none([m["total_tokens"] for m in models]),
            "estimated_cost_usd": round(sum(costs), 6) if costs else None,
            "models": models,
            "tools": tools,
        }
