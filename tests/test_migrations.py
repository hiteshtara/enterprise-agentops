"""Alembic migration behaviour, exercised through its Python API.

These tests never shell out: they build a temporary SQLite database, run the
migration chain against it in-process, and inspect the result.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from alembic import command
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.database import Database
from app.identity import Role
from app.run_store import RunStore
from app.seed_data import DEVELOPMENT_BATCHES, seed_migration_batches
from app.seed_users import DEMO_USERS, seed_demo_users
from app.user_store import UserStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "approvals",
    "audit_events",
    "migration_batches",
    "run_steps",
    "runs",
    "users",
}

EXPECTED_INDEXES = {
    "approvals": {
        "ix_approvals_run_id",
        "ix_approvals_requested_by_user_id",
        "ix_approvals_resolved_by_user_id",
        "ix_approvals_status",
    },
    "audit_events": {"ix_audit_events_run_id", "ix_audit_events_actor_user_id"},
    "migration_batches": {
        "ix_migration_batches_batch_id",
        "ix_migration_batches_status",
    },
    "run_steps": {"ix_run_steps_run_id"},
    "runs": {"ix_runs_status", "ix_runs_requested_by_user_id"},
    "users": {"ix_users_email", "ix_users_role"},
}


def alembic_config(database: Database) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))

    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database.url)

    return config


@pytest.fixture
def migrated(tmp_path) -> Database:
    """A temporary database brought to head by Alembic, not by create_all()."""
    database = Database(url=f"sqlite:///{tmp_path / 'migrated.db'}")

    command.upgrade(alembic_config(database), "head")

    yield database

    database.dispose()


# -- upgrading -------------------------------------------------------------


def test_a_fresh_database_upgrades_from_zero_to_head(migrated):
    tables = set(inspect(migrated.engine).get_table_names())

    assert EXPECTED_TABLES <= tables


def test_alembic_version_is_recorded_at_head(migrated, tmp_path):
    inspector = inspect(migrated.engine)

    assert "alembic_version" in inspector.get_table_names()

    script = ScriptDirectory.from_config(alembic_config(migrated))

    with migrated.session() as session:
        from sqlalchemy import text

        stamped = session.execute(
            text("select version_num from alembic_version")
        ).scalar()

    assert stamped == script.get_current_head()


def test_expected_indexes_exist(migrated):
    inspector = inspect(migrated.engine)

    for table, expected in EXPECTED_INDEXES.items():
        found = {index["name"] for index in inspector.get_indexes(table)}

        assert expected <= found, f"{table}: missing {expected - found}"


def test_unique_constraints_are_enforced(migrated):
    inspector = inspect(migrated.engine)

    def unique(table: str, name: str) -> bool:
        return any(
            index["name"] == name and index["unique"]
            for index in inspector.get_indexes(table)
        )

    assert unique("users", "ix_users_email")
    assert unique("migration_batches", "ix_migration_batches_batch_id")


def test_identity_columns_are_present_and_nullable(migrated):
    inspector = inspect(migrated.engine)

    def column(table: str, name: str):
        return next(c for c in inspector.get_columns(table) if c["name"] == name)

    assert column("runs", "requested_by_user_id")["nullable"]
    assert column("approvals", "requested_by_user_id")["nullable"]
    assert column("approvals", "resolved_by_user_id")["nullable"]
    assert column("audit_events", "actor_user_id")["nullable"]


def test_upgrade_to_head_is_idempotent(migrated):
    config = alembic_config(migrated)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert EXPECTED_TABLES <= set(inspect(migrated.engine).get_table_names())


def test_downgrade_removes_the_schema(migrated):
    command.downgrade(alembic_config(migrated), "base")

    remaining = set(inspect(migrated.engine).get_table_names())

    assert not (EXPECTED_TABLES & remaining)


def test_there_is_a_single_head(migrated):
    """A branched history would make `upgrade head` ambiguous."""
    script = ScriptDirectory.from_config(alembic_config(migrated))

    assert len(script.get_heads()) == 1


# -- migrations create structure only --------------------------------------


def test_migrations_seed_no_data(migrated):
    for table in EXPECTED_TABLES:
        from sqlalchemy import text

        with migrated.session() as session:
            count = session.execute(text(f"select count(*) from {table}")).scalar()

        assert count == 0, f"{table} was seeded by a migration"


def test_seeding_works_after_migration(migrated, monkeypatch):
    monkeypatch.setenv("AGENTGUARD_BCRYPT_ROUNDS", "4")

    assert seed_migration_batches(migrated) == len(DEVELOPMENT_BATCHES)
    assert seed_demo_users(migrated) == len(DEMO_USERS)

    assert len(UserStore(database=migrated).list_users()) == len(DEMO_USERS)


# -- the application works against a migrated database ---------------------


def test_stores_operate_against_a_migrated_database(migrated, monkeypatch):
    monkeypatch.setenv("AGENTGUARD_BCRYPT_ROUNDS", "4")

    run_store = RunStore(database=migrated)
    approval_store = ApprovalStore(database=migrated)
    audit_store = AuditStore(database=migrated)
    user_store = UserStore(database=migrated)

    user = user_store.create(
        email="someone@agentguard.local",
        display_name="Someone",
        password="a-valid-password",
        role=Role.APPROVER,
    )

    run_id = run_store.create_run(
        "Investigate batch 43.", requested_by_user_id=user.user_id
    )

    approval = approval_store.create(
        tool="restart_migration",
        arguments={"batch_id": 43},
        risk="WRITE",
        run_id=run_id,
        tool_call_id="call-1",
        requested_by_user_id=user.user_id,
    )

    approval_store.resolve(
        approval.approval_id, approved=True, resolved_by_user_id=user.user_id
    )

    audit_store.record(
        "TOOL_EXECUTED",
        {"tool": "restart_migration"},
        run_id=run_id,
        actor_user_id=user.user_id,
    )

    run_store.complete(run_id, "Restarted.")

    assert run_store.get_run(run_id).status == "COMPLETED"
    assert approval_store.get(approval.approval_id).resolved_by_user_id == user.user_id
    assert audit_store.list_events(run_id=run_id)[0]["actor_user_id"] == user.user_id


# -- migrations agree with the models --------------------------------------


def test_the_migrated_schema_matches_the_models(migrated, tmp_path):
    """No drift between `alembic upgrade head` and `Base.metadata`."""
    from_models = Database(url=f"sqlite:///{tmp_path / 'from_models.db'}")
    from_models.create_all()

    def describe(database: Database) -> dict:
        inspector = inspect(database.engine)

        return {
            table: {
                "columns": sorted(
                    (c["name"], str(c["type"]), c["nullable"])
                    for c in inspector.get_columns(table)
                ),
                "indexes": sorted(
                    (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                    for i in inspector.get_indexes(table)
                ),
                "primary_key": inspector.get_pk_constraint(table)[
                    "constrained_columns"
                ],
            }
            for table in sorted(EXPECTED_TABLES)
        }

    assert describe(migrated) == describe(from_models)

    from_models.dispose()


def test_a_caller_supplied_url_beats_the_environment(tmp_path, monkeypatch):
    """env.py must not redirect a programmatic migration at the default DB.

    Regression guard: env.py once set sqlalchemy.url unconditionally, so a test
    pointing Alembic at its own file silently migrated ./agentops.db instead.
    """
    monkeypatch.setenv("AGENTOPS_DATABASE_URL", "sqlite:///should-not-be-used.db")

    target = Database(url=f"sqlite:///{tmp_path / 'explicit.db'}")

    command.upgrade(alembic_config(target), "head")

    assert EXPECTED_TABLES <= set(inspect(target.engine).get_table_names())
    assert not Path("should-not-be-used.db").exists()

    target.dispose()


def test_the_development_database_is_untouched(migrated, development_database_path):
    """Running migrations in a test must never reach ./agentops.db."""
    assert "agentops.db" not in migrated.url

    if development_database_path.exists():
        before = development_database_path.stat()

        command.upgrade(alembic_config(migrated), "head")

        after = development_database_path.stat()

        assert (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        )
