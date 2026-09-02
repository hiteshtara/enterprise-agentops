from enum import Enum
from typing import Any

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import MigrationBatchRecord


class MigrationStatus(str, Enum):
    """The only status values a caller may filter on."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RUNNING = "RUNNING"
    PENDING = "PENDING"


ALLOWED_STATUSES: tuple[str, ...] = tuple(status.value for status in MigrationStatus)

DEFAULT_LIMIT = 20

MIN_LIMIT = 1

MAX_LIMIT = 100


def validate_status(status: str | None) -> str | None:
    """Return the status unchanged, or raise if it is not an allowed value."""
    if status is None:
        return None

    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"Unsupported status: {status!r}. "
            f"Allowed values: {', '.join(ALLOWED_STATUSES)}."
        )

    return status


def validate_limit(limit: int) -> int:
    """Return the limit unchanged, or raise if it is not a valid limit.

    Raises:
        TypeError: If limit is not an integer.
        ValueError: If limit is outside [MIN_LIMIT, MAX_LIMIT].
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit must be an integer, got {type(limit).__name__}.")

    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise ValueError(
            f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}, got {limit}."
        )

    return limit


def to_dict(record: MigrationBatchRecord) -> dict[str, Any]:
    return {
        "batch_id": record.batch_id,
        "status": record.status,
        "records": record.records,
        "duration_seconds": record.duration_seconds,
        "error": record.error,
        "created_at": record.created_at,
    }


class MigrationBatchStore:
    """Read-only access to authoritative migration batch records.

    Every query is composed from SQLAlchemy expressions here in Python. No
    caller -- and specifically not the language model -- supplies SQL text,
    column names, table names, or ordering clauses.
    """

    def __init__(
        self,
        database: Database | None = None,
    ) -> None:
        self._database = database or get_database()

    def query(
        self,
        status: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return migration batches, newest first.

        Args:
            status: Optional filter; must be one of ALLOWED_STATUSES.
            limit: Maximum rows to return, between MIN_LIMIT and MAX_LIMIT.

        Raises:
            ValueError: If status or limit is outside the allowed domain.
        """
        validated_status = validate_status(status)
        validated_limit = validate_limit(limit)

        statement = select(MigrationBatchRecord)

        if validated_status is not None:
            statement = statement.where(MigrationBatchRecord.status == validated_status)

        statement = statement.order_by(
            MigrationBatchRecord.created_at.desc(),
            MigrationBatchRecord.batch_id.desc(),
        ).limit(validated_limit)

        with self._database.session() as session:
            records = session.scalars(statement).all()

            return [to_dict(record) for record in records]
