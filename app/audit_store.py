import json
from typing import Any

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import AuditEventRecord


class AuditStore:
    def __init__(
        self,
        database: Database | None = None,
    ) -> None:
        self._database = database or get_database()

    def record(
        self,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        event = AuditEventRecord(
            event_type=event_type,
            details_json=json.dumps(details),
        )

        with self._database.session() as session:
            session.add(event)
            session.commit()

    def list_events(
        self,
    ) -> list[dict[str, Any]]:
        with self._database.session() as session:
            statement = select(AuditEventRecord).order_by(AuditEventRecord.id.desc())

            events = session.scalars(statement).all()

            return [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "details": json.loads(event.details_json),
                    "created_at": event.created_at,
                }
                for event in events
            ]
