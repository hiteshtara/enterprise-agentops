import json
from datetime import UTC, datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PendingApprovalRecord(Base):
    __tablename__ = "pending_approvals"

    approval_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
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

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
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
