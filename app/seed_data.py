"""Explicit, idempotent development seed data for migration batches.

Nothing here runs on import. Call seed_migration_batches() deliberately, from
the init_db entry point or from a test against its own isolated database.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import MigrationBatchRecord
from app.migration_store import MigrationStatus

SEED_BASE_TIME = datetime(2026, 3, 14, 9, 0, 0, tzinfo=UTC)

SEED_INTERVAL_MINUTES = 37

# (batch_id, status, records, duration_seconds, error) -- oldest first.
DEVELOPMENT_BATCHES: tuple[tuple[int, MigrationStatus, int, int, str | None], ...] = (
    (41, MigrationStatus.SUCCESS, 499, 38, None),
    (42, MigrationStatus.SUCCESS, 498, 41, None),
    (43, MigrationStatus.FAILED, 495, 12, "Oracle connection timeout"),
    (44, MigrationStatus.SUCCESS, 497, 39, None),
    (45, MigrationStatus.SUCCESS, 500, 44, None),
    (46, MigrationStatus.FAILED, 210, 8, "ORA-01555: snapshot too old"),
    (47, MigrationStatus.SUCCESS, 496, 37, None),
    (48, MigrationStatus.SUCCESS, 501, 42, None),
    (49, MigrationStatus.FAILED, 88, 5, "Constraint violation on CUSTOMER_ID"),
    (50, MigrationStatus.SUCCESS, 499, 40, None),
    (51, MigrationStatus.SUCCESS, 494, 36, None),
    (52, MigrationStatus.SUCCESS, 502, 43, None),
    (53, MigrationStatus.FAILED, 0, 2, "Target schema ORDERS_V2 unavailable"),
    (54, MigrationStatus.SUCCESS, 498, 39, None),
    (55, MigrationStatus.SUCCESS, 500, 41, None),
    (56, MigrationStatus.SUCCESS, 497, 38, None),
    (57, MigrationStatus.FAILED, 143, 9, "Deadlock detected on STAGING_CUSTOMER"),
    (58, MigrationStatus.SUCCESS, 503, 45, None),
    (59, MigrationStatus.SUCCESS, 495, 37, None),
    (60, MigrationStatus.SUCCESS, 499, 40, None),
    (61, MigrationStatus.FAILED, 61, 4, "Source extract produced malformed UTF-8"),
    (62, MigrationStatus.SUCCESS, 501, 42, None),
    (63, MigrationStatus.SUCCESS, 496, 38, None),
    (64, MigrationStatus.RUNNING, 318, 27, None),
)


def seed_created_at(index: int) -> str:
    """Deterministic timestamp so ordering is stable and re-seeding is a no-op."""
    minutes_ago = (len(DEVELOPMENT_BATCHES) - 1 - index) * SEED_INTERVAL_MINUTES

    return (SEED_BASE_TIME - timedelta(minutes=minutes_ago)).isoformat()


def seed_migration_batches(
    database: Database | None = None,
) -> int:
    """Insert any missing development batches. Returns the number inserted.

    Existing rows are never updated or deleted, so re-running is safe.
    """
    target = database or get_database()

    with target.session() as session:
        existing = set(session.scalars(select(MigrationBatchRecord.batch_id)).all())

        inserted = 0

        for index, spec in enumerate(DEVELOPMENT_BATCHES):
            batch_id, status, records, duration_seconds, error = spec

            if batch_id in existing:
                continue

            session.add(
                MigrationBatchRecord(
                    batch_id=batch_id,
                    status=status.value,
                    records=records,
                    duration_seconds=duration_seconds,
                    error=error,
                    created_at=seed_created_at(index),
                )
            )

            inserted += 1

        session.commit()

    return inserted
