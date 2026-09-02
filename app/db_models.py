import json
from datetime import UTC, datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ApprovalRecord(Base):
    """One approval decision, kept after resolution so history is queryable."""

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )

    tool_call_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    tool: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    arguments_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    risk: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    resolved_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    decision: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    @property
    def arguments(self) -> dict:
        return json.loads(self.arguments_json)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    run_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    details_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )


class MigrationBatchRecord(Base):
    __tablename__ = "migration_batches"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    batch_id: Mapped[int] = mapped_column(
        unique=True,
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    records: Mapped[int] = mapped_column(nullable=False)

    duration_seconds: Mapped[int] = mapped_column(nullable=False)

    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )


class RunRecord(Base):
    """One agent request, durable across restarts and approval waits."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    user_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    final_answer: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    conversation_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    updated_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    @property
    def conversation(self) -> list:
        return json.loads(self.conversation_json)


class RunStepRecord(Base):
    """Execution history for replay and resumption.

    Distinct from AuditEventRecord, which is the compliance record. A step is
    what the runtime did; an audit event is what a reviewer needs to see.
    """

    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )

    step_number: Mapped[int] = mapped_column(nullable=False)

    step_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    tool_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    arguments_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    result_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    error_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )
