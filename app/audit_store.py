import json
from typing import Any

from sqlalchemy import func, select

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
        actor_user_id: str | None = None,
    ) -> None:
        event = AuditEventRecord(
            event_type=event_type,
            details_json=json.dumps(details),
            run_id=run_id,
            actor_user_id=actor_user_id,
        )

        with self._database.session() as session:
            session.add(event)
            session.commit()

    def list_events(
        self,
        run_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Newest first. Pass run_id to scope the timeline to a single run."""
        statement = select(AuditEventRecord)

        if run_id is not None:
            statement = statement.where(AuditEventRecord.run_id == run_id)

        if event_type is not None:
            statement = statement.where(AuditEventRecord.event_type == event_type)

        statement = statement.order_by(AuditEventRecord.id.desc())

        if limit is not None:
            statement = statement.limit(limit)

        with self._database.session() as session:
            events = session.scalars(statement).all()

            return [
                {
                    "id": event.id,
                    "run_id": event.run_id,
                    "actor_user_id": event.actor_user_id,
                    "event_type": event.event_type,
                    "details": json.loads(event.details_json),
                    "created_at": event.created_at,
                }
                for event in events
            ]

    def count_by_type(self) -> dict[str, int]:
        statement = select(
            AuditEventRecord.event_type,
            func.count(AuditEventRecord.id),
        ).group_by(AuditEventRecord.event_type)

        with self._database.session() as session:
            return dict(session.execute(statement).all())
