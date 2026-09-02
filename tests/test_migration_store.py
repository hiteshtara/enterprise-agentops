import pytest

from app.migration_store import (
    ALLOWED_STATUSES,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    MigrationBatchStore,
    MigrationStatus,
)
from app.seed_data import DEVELOPMENT_BATCHES, seed_migration_batches


def test_query_without_status_returns_batches_of_every_status(migration_store):
    results = migration_store.query(limit=MAX_LIMIT)

    assert len(results) == len(DEVELOPMENT_BATCHES)

    statuses = {row["status"] for row in results}

    assert statuses == {"SUCCESS", "FAILED", "RUNNING"}


def test_query_filters_failed_batches(migration_store):
    results = migration_store.query(
        status=MigrationStatus.FAILED.value, limit=MAX_LIMIT
    )

    assert results
    assert all(row["status"] == "FAILED" for row in results)
    assert all(row["error"] is not None for row in results)

    expected = sum(
        1 for spec in DEVELOPMENT_BATCHES if spec[1] is MigrationStatus.FAILED
    )

    assert len(results) == expected


def test_query_filters_successful_batches(migration_store):
    results = migration_store.query(
        status=MigrationStatus.SUCCESS.value,
        limit=MAX_LIMIT,
    )

    assert results
    assert all(row["status"] == "SUCCESS" for row in results)
    assert all(row["error"] is None for row in results)


def test_results_are_newest_first(migration_store):
    results = migration_store.query(limit=MAX_LIMIT)

    timestamps = [row["created_at"] for row in results]

    assert timestamps == sorted(timestamps, reverse=True)

    # The seed data's newest row is the highest batch_id.
    assert results[0]["batch_id"] == max(spec[0] for spec in DEVELOPMENT_BATCHES)


def test_default_limit_is_applied(migration_store):
    results = migration_store.query()

    assert DEFAULT_LIMIT < len(DEVELOPMENT_BATCHES), (
        "Seed data must exceed the default limit for this test to be meaningful."
    )
    assert len(results) == DEFAULT_LIMIT


def test_explicit_limit_is_applied(migration_store):
    results = migration_store.query(limit=5)

    assert len(results) == 5


def test_explicit_limit_combines_with_status_filter(migration_store):
    results = migration_store.query(status="SUCCESS", limit=3)

    assert len(results) == 3
    assert all(row["status"] == "SUCCESS" for row in results)


def test_limit_below_minimum_is_rejected(migration_store):
    with pytest.raises(ValueError, match="between 1 and 100"):
        migration_store.query(limit=MIN_LIMIT - 1)


def test_limit_above_maximum_is_rejected(migration_store):
    with pytest.raises(ValueError, match="between 1 and 100"):
        migration_store.query(limit=MAX_LIMIT + 1)


def test_boundary_limits_are_accepted(migration_store):
    assert len(migration_store.query(limit=MIN_LIMIT)) == MIN_LIMIT
    assert len(migration_store.query(limit=MAX_LIMIT)) == len(DEVELOPMENT_BATCHES)


def test_non_integer_limit_is_rejected(migration_store):
    with pytest.raises(TypeError, match="must be an integer"):
        migration_store.query(limit="20")


def test_invalid_status_is_rejected(migration_store):
    with pytest.raises(ValueError, match="Unsupported status"):
        migration_store.query(status="DROP TABLE migration_batches")


def test_lowercase_status_is_rejected(migration_store):
    with pytest.raises(ValueError, match="Unsupported status"):
        migration_store.query(status="failed")


def test_valid_status_with_no_matching_rows_returns_empty_list(migration_store):
    # PENDING is an allowed status but is absent from the seed data.
    assert "PENDING" in ALLOWED_STATUSES

    assert migration_store.query(status="PENDING") == []


def test_query_on_empty_database_returns_empty_list(database):
    assert MigrationBatchStore(database=database).query() == []


def test_returned_rows_have_the_expected_shape(migration_store):
    row = migration_store.query(limit=1)[0]

    assert set(row) == {
        "batch_id",
        "status",
        "records",
        "duration_seconds",
        "error",
        "created_at",
    }
    assert isinstance(row["batch_id"], int)
    assert isinstance(row["records"], int)
    assert isinstance(row["duration_seconds"], int)


def test_seeding_is_idempotent(database):
    first = seed_migration_batches(database)
    second = seed_migration_batches(database)

    assert first == len(DEVELOPMENT_BATCHES)
    assert second == 0

    store = MigrationBatchStore(database=database)

    assert len(store.query(limit=MAX_LIMIT)) == len(DEVELOPMENT_BATCHES)


def test_seeding_does_not_run_on_import(database):
    # The fixture only creates the schema; seeding must be an explicit call.
    assert MigrationBatchStore(database=database).query() == []
