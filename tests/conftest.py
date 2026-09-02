from pathlib import Path

import pytest

from app.database import DEFAULT_DATABASE_URL, Database
from app.migration_store import MigrationBatchStore
from app.seed_data import seed_migration_batches
from app.tool_registry import ToolRegistry
from app.tool_setup import build_tool_registry


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """An isolated, schema-initialised SQLite database for a single test.

    pytest's tmp_path is unique per test, so every test starts empty and
    nothing is written to the development database.
    """
    database = Database(url=f"sqlite:///{tmp_path / 'isolated.db'}")

    database.create_all()

    yield database

    database.dispose()


@pytest.fixture
def development_database_path() -> Path:
    """Path to the local development database referenced by the default URL."""
    return Path(DEFAULT_DATABASE_URL.removeprefix("sqlite:///"))


@pytest.fixture
def seeded_database(database: Database) -> Database:
    """An isolated database with the development migration batches loaded."""
    seed_migration_batches(database)

    return database


@pytest.fixture
def migration_store(seeded_database: Database) -> MigrationBatchStore:
    return MigrationBatchStore(database=seeded_database)


@pytest.fixture
def registry(migration_store: MigrationBatchStore) -> ToolRegistry:
    """The real application tool registry, backed by the isolated database."""
    return build_tool_registry(migration_store=migration_store)
