from pathlib import Path

import pytest

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.database import DEFAULT_DATABASE_URL, Database
from app.model_provider import ModelProvider
from app.tool_registry import Tool, ToolRegistry, ToolRisk
from app.tools import restart_migration


class UnusedModelProvider(ModelProvider):
    """resolve_approval must never call the model; this fails loudly if it does."""

    def generate(self, message: str) -> str:
        raise AssertionError("The model must not be called.")

    def generate_with_tools(self, input_items, tools):
        raise AssertionError("The model must not be called.")


def snapshot(path):
    """Capture enough file state to detect any write to the development database."""
    if not path.exists():
        return None

    stat = path.stat()

    return (stat.st_size, stat.st_mtime_ns)


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="restart_migration",
            description="Restart migration.",
            function=restart_migration,
            parameters={},
            risk=ToolRisk.WRITE,
        )
    )

    return registry


def test_approval_persists_in_configured_database(database):
    store = ApprovalStore(database=database)

    created = store.create(
        tool="restart_migration",
        arguments={"batch_id": 43},
        risk="WRITE",
    )

    # A second store on the same Database reads the row back, proving it was
    # committed rather than held in a single session's identity map.
    reloaded = ApprovalStore(database=database).get(created.approval_id)

    assert reloaded is not None
    assert reloaded.tool == "restart_migration"
    assert reloaded.arguments == {"batch_id": 43}
    assert reloaded.risk == "WRITE"


def test_audit_events_persist_in_configured_database(database):
    store = AuditStore(database=database)

    store.record("TOOL_REQUESTED", {"tool": "restart_migration"})
    store.record("TOOL_EXECUTED", {"tool": "restart_migration", "result": "ok"})

    events = AuditStore(database=database).list_events()

    assert [event["event_type"] for event in events] == [
        "TOOL_EXECUTED",
        "TOOL_REQUESTED",
    ]
    assert events[0]["details"]["result"] == "ok"


def test_each_test_starts_from_an_empty_database(database):
    # Runs after the two tests above; a leaked database would carry their rows.
    assert AuditStore(database=database).list_events() == []


def test_database_fixture_is_isolated_from_development_database(
    database,
    development_database_path,
):
    assert database.url != DEFAULT_DATABASE_URL

    fixture_path = Path(database.url.removeprefix("sqlite:///"))

    assert fixture_path.resolve() != development_database_path.resolve()

    before = snapshot(development_database_path)

    ApprovalStore(database=database).create(
        tool="restart_migration",
        arguments={"batch_id": 99},
        risk="WRITE",
    )
    AuditStore(database=database).record("TOOL_REQUESTED", {"tool": "x"})

    assert snapshot(development_database_path) == before


def test_removing_an_approval_deletes_the_row(database):
    store = ApprovalStore(database=database)

    created = store.create(
        tool="restart_migration",
        arguments={"batch_id": 43},
        risk="WRITE",
    )

    store.remove(created.approval_id)

    assert store.get(created.approval_id) is None

    # remove() is idempotent: a second call must not raise.
    store.remove(created.approval_id)


def test_resolving_an_approval_executes_the_tool_and_clears_it(database):
    approval_store = ApprovalStore(database=database)
    audit_store = AuditStore(database=database)

    agent = AgentService(
        model=UnusedModelProvider(),
        tool_registry=build_registry(),
        approval_store=approval_store,
        audit_store=audit_store,
    )

    pending = approval_store.create(
        tool="restart_migration",
        arguments={"batch_id": 43},
        risk="WRITE",
    )

    result = agent.resolve_approval(
        approval_id=pending.approval_id,
        approved=True,
    )

    assert result["approved"] is True
    assert result["tool"] == "restart_migration"
    assert result["result"]["status"] == "RESTARTED"

    assert approval_store.get(pending.approval_id) is None

    event_types = [event["event_type"] for event in audit_store.list_events()]

    assert "APPROVAL_GRANTED" in event_types
    assert "TOOL_EXECUTED" in event_types


def test_denying_an_approval_clears_it_without_executing(database):
    approval_store = ApprovalStore(database=database)
    audit_store = AuditStore(database=database)

    agent = AgentService(
        model=UnusedModelProvider(),
        tool_registry=build_registry(),
        approval_store=approval_store,
        audit_store=audit_store,
    )

    pending = approval_store.create(
        tool="restart_migration",
        arguments={"batch_id": 43},
        risk="WRITE",
    )

    result = agent.resolve_approval(
        approval_id=pending.approval_id,
        approved=False,
    )

    assert result["approved"] is False
    assert result["result"] is None

    assert approval_store.get(pending.approval_id) is None

    event_types = [event["event_type"] for event in audit_store.list_events()]

    assert "APPROVAL_DENIED" in event_types
    assert "TOOL_EXECUTED" not in event_types


def test_unknown_approval_id_is_rejected(database):
    agent = AgentService(
        model=UnusedModelProvider(),
        tool_registry=build_registry(),
        approval_store=ApprovalStore(database=database),
        audit_store=AuditStore(database=database),
    )

    with pytest.raises(ValueError, match="does-not-exist"):
        agent.resolve_approval(approval_id="does-not-exist", approved=True)


def test_two_databases_do_not_share_rows(tmp_path):
    first = Database(url=f"sqlite:///{tmp_path / 'first.db'}")
    second = Database(url=f"sqlite:///{tmp_path / 'second.db'}")

    first.create_all()
    second.create_all()

    AuditStore(database=first).record("TOOL_REQUESTED", {"tool": "a"})

    assert len(AuditStore(database=first).list_events()) == 1
    assert AuditStore(database=second).list_events() == []

    first.dispose()
    second.dispose()


def test_create_all_is_idempotent(tmp_path):
    database = Database(url=f"sqlite:///{tmp_path / 'idempotent.db'}")

    database.create_all()

    AuditStore(database=database).record("TOOL_REQUESTED", {"tool": "a"})

    database.create_all()

    assert len(AuditStore(database=database).list_events()) == 1

    database.dispose()
