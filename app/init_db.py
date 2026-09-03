"""Schema and seed helpers.

**Alembic is authoritative for schema evolution.** `uv run alembic upgrade head`
is how a real database -- local, staging or production -- gets its structure.

`Database.create_all()` remains available for *ephemeral* databases only: the
per-test SQLite files built and thrown away by the pytest fixtures, where
running a migration chain would buy nothing and slow every test down.

Never use create_all() to evolve a database that holds data you intend to keep;
it creates missing tables but cannot add a column to an existing one, which is
exactly how this project earned two manual database resets before adopting
Alembic.
"""

from app.database import Database, get_database
from app.seed_data import seed_migration_batches
from app.seed_users import seed_demo_users


def create_schema_for_tests(
    database: Database | None = None,
) -> None:
    """Build the schema directly from the models, for a throwaway database."""
    (database or get_database()).create_all()


def seed_development_data(
    database: Database | None = None,
) -> int:
    """Insert demo batches and demo users. Idempotent; never run by a migration."""
    target = database or get_database()

    return seed_migration_batches(target) + seed_demo_users(target)


def init_database(
    database: Database | None = None,
    seed: bool = False,
) -> int:
    """Deprecated: use `alembic upgrade head`, then `python -m app.seed_data`.

    Kept as a compatibility wrapper so existing callers and older documentation
    keep working. It still creates the schema with create_all(), which is only
    correct for a fresh or ephemeral database.
    """
    target = database or get_database()

    create_schema_for_tests(target)

    if not seed:
        return 0

    return seed_development_data(target)


if __name__ == "__main__":
    print(
        "app.init_db is deprecated for real databases.\n"
        "  Schema:  uv run alembic upgrade head\n"
        "  Seed:    uv run python -m app.seed_data"
    )
