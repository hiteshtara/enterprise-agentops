"""Approval persistence.

Rows are never deleted. An approval keeps its history after resolution so the
console can show Pending / Approved / Rejected.
"""

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import ApprovalRecord


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


def approval_to_dict(record: ApprovalRecord) -> dict[str, Any]:
    return {
        "approval_id": record.approval_id,
        "run_id": record.run_id,
        "tool": record.tool,
        "arguments": record.arguments,
        "risk": record.risk,
        "status": record.status,
        "created_at": record.created_at,
        "resolved_at": record.resolved_at,
        "decision": record.decision,
    }


class ApprovalStore:
    def __init__(
        self,
        database: Database | None = None,
    ) -> None:
        self._database = database or get_database()

    def create(
        self,
        tool: str,
        arguments: dict,
        risk: str,
        run_id: str,
        tool_call_id: str,
    ) -> ApprovalRecord:
        approval = ApprovalRecord(
            approval_id=str(uuid4()),
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool=tool,
            arguments_json=json.dumps(arguments),
            risk=risk,
            status=ApprovalStatus.PENDING.value,
            created_at=datetime.now(UTC).isoformat(),
            resolved_at=None,
            decision=None,
        )

        with self._database.session() as session:
            session.add(approval)
            session.commit()
            session.refresh(approval)

        return approval

    def get(
        self,
        approval_id: str,
    ) -> ApprovalRecord | None:
        with self._database.session() as session:
            statement = select(ApprovalRecord).where(
                ApprovalRecord.approval_id == approval_id
            )

            return session.scalar(statement)

    def resolve(
        self,
        approval_id: str,
        approved: bool,
    ) -> ApprovalRecord:
        """Record the decision. The row is kept, not deleted."""
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED

        with self._database.session() as session:
            approval = session.get(ApprovalRecord, approval_id)

            if approval is None:
                raise ValueError(f"Unknown approval ID: {approval_id}")

            approval.status = status.value
            approval.decision = status.value
            approval.resolved_at = datetime.now(UTC).isoformat()

            session.commit()
            session.refresh(approval)

            return approval

    def list_approvals(
        self,
        status: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(ApprovalRecord)

        if status is not None:
            statement = statement.where(ApprovalRecord.status == status)

        if run_id is not None:
            statement = statement.where(ApprovalRecord.run_id == run_id)

        statement = statement.order_by(ApprovalRecord.created_at.desc())

        with self._database.session() as session:
            return [approval_to_dict(record) for record in session.scalars(statement)]
