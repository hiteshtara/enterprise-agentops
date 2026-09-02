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
        run_id: str | None = None,
    ) -> None:
        event = AuditEventRecord(
            event_type=event_type,
            details_json=json.dumps(details),
            run_id=run_id,
        )

        with self._database.session() as session:
            session.add(event)
            session.commit()

    def list_events(
        self,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest first. Pass run_id to scope the timeline to a single run."""
        statement = select(AuditEventRecord)

        if run_id is not None:
            statement = statement.where(AuditEventRecord.run_id == run_id)

        statement = statement.order_by(AuditEventRecord.id.desc())

        with self._database.session() as session:
            events = session.scalars(statement).all()

            return [
                {
                    "id": event.id,
                    "run_id": event.run_id,
                    "event_type": event.event_type,
                    "details": json.loads(event.details_json),
                    "created_at": event.created_at,
                }
                for event in events
            ]
