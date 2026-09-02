import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import PendingApprovalRecord


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
    ) -> PendingApprovalRecord:
        approval = PendingApprovalRecord(
            approval_id=str(uuid4()),
            tool=tool,
            arguments_json=json.dumps(arguments),
            risk=risk,
            created_at=datetime.now(UTC).isoformat(),
        )

        with self._database.session() as session:
            session.add(approval)
            session.commit()
            session.refresh(approval)

        return approval

    def get(
        self,
        approval_id: str,
    ) -> PendingApprovalRecord | None:
        with self._database.session() as session:
            statement = select(PendingApprovalRecord).where(
                PendingApprovalRecord.approval_id == approval_id
            )

            return session.scalar(statement)

    def remove(
        self,
        approval_id: str,
    ) -> None:
        with self._database.session() as session:
            approval = session.get(
                PendingApprovalRecord,
                approval_id,
            )

            if approval is not None:
                session.delete(approval)
                session.commit()
