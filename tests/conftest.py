from pathlib import Path

import pytest

from app.database import DEFAULT_DATABASE_URL, Database


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
