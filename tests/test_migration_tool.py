from types import SimpleNamespace

import pytest

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.migration_store import (
    ALLOWED_STATUSES,
    MAX_LIMIT,
    MIN_LIMIT,
)
from app.model_provider import ModelProvider
from app.seed_data import DEVELOPMENT_BATCHES
from app.tool_registry import ToolRisk
from app.tool_setup import build_tool_registry

TOOL_NAME = "query_migration_batches"


class FakeQueryModelProvider(ModelProvider):
    """Requests the database tool once, then answers from the tool output."""

    def __init__(self, arguments: str = '{"status": "FAILED", "limit": 5}') -> None:
        self.arguments = arguments
        self.call_count = 0
        self.tool_output_seen: str | None = None

    def generate(self, message: str) -> str:
        raise AssertionError("generate() must not be used by the agent loop.")

    def generate_with_tools(self, input_items, tools):
        self.call_count += 1

        if self.call_count == 1:
            return SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name=TOOL_NAME,
                        arguments=self.arguments,
                        call_id="query-call-1",
                    )
                ],
                output_text="",
            )

        for item in input_items:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                self.tool_output_seen = item["output"]

        return SimpleNamespace(
            output=[],
            output_text="Batches 43, 46, 49, 53 and 57 failed.",
        )


@pytest.fixture
def registry(migration_store):
    return build_tool_registry(migration_store=migration_store)


def test_tool_is_registered_as_read(registry):
    schema_names = [schema["name"] for schema in registry.schemas()]

    assert TOOL_NAME in schema_names

    assert registry._tools[TOOL_NAME].risk is ToolRisk.READ


def test_tool_requires_no_approval(registry):
    # A READ tool executes without approved=True.
    result = registry.execute(TOOL_NAME, {"limit": 3})

    assert len(result) == 3


def test_tool_schema_constrains_status_and_limit(registry):
    schema = next(s for s in registry.schemas() if s["name"] == TOOL_NAME)

    parameters = schema["parameters"]

    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"status", "limit"}

    status = parameters["properties"]["status"]

    assert status["type"] == "string"
    assert status["enum"] == list(ALLOWED_STATUSES)

    limit = parameters["properties"]["limit"]

    assert limit["type"] == "integer"
    assert limit["minimum"] == MIN_LIMIT
    assert limit["maximum"] == MAX_LIMIT


def test_tool_schema_exposes_no_sql_surface(registry):
    """The model must have no way to supply SQL, columns, tables or ordering."""
    schema = next(s for s in registry.schemas() if s["name"] == TOOL_NAME)

    properties = schema["parameters"]["properties"]

    forbidden = {
        "sql",
        "query",
        "where",
        "order_by",
        "columns",
        "table",
        "filter",
        "expression",
    }

    assert forbidden.isdisjoint(properties)

    for definition in properties.values():
        assert definition["type"] in {"string", "integer"}

        if definition["type"] == "string":
            assert "enum" in definition, "Free-text string arguments are not allowed."


def test_registry_executes_the_tool_with_a_status_filter(registry):
    results = registry.execute(TOOL_NAME, {"status": "FAILED", "limit": 10})

    assert results
    assert all(row["status"] == "FAILED" for row in results)


def test_registry_rejects_invalid_status_through_the_tool(registry):
    with pytest.raises(ValueError, match="Unsupported status"):
        registry.execute(TOOL_NAME, {"status": "ARBITRARY"})


def test_registry_rejects_out_of_range_limit_through_the_tool(registry):
    with pytest.raises(ValueError, match="between 1 and 100"):
        registry.execute(TOOL_NAME, {"limit": 500})


def test_agent_executes_the_database_tool(registry, seeded_database):
    model = FakeQueryModelProvider()

    agent = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=AuditStore(database=seeded_database),
    )

    result = agent.run("Show me failed migration batches.")

    assert result["approval_required"] is None
    assert result["answer"] == "Batches 43, 46, 49, 53 and 57 failed."
    assert model.call_count == 2


def test_execution_trace_contains_the_database_tool(registry, seeded_database):
    agent = AgentService(
        model=FakeQueryModelProvider(),
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=AuditStore(database=seeded_database),
    )

    result = agent.run("Show me failed migration batches.")

    assert len(result["trace"]) == 1

    step = result["trace"][0]

    assert step["tool"] == TOOL_NAME
    assert step["arguments"] == {"status": "FAILED", "limit": 5}
    assert all(row["status"] == "FAILED" for row in step["result"])


def test_audit_records_tool_requested_and_executed(registry, seeded_database):
    audit_store = AuditStore(database=seeded_database)

    agent = AgentService(
        model=FakeQueryModelProvider(),
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
    )

    agent.run("Show me failed migration batches.")

    events = audit_store.list_events()

    by_type = {event["event_type"]: event for event in events}

    assert "TOOL_REQUESTED" in by_type
    assert "TOOL_EXECUTED" in by_type

    assert by_type["TOOL_REQUESTED"]["details"]["tool"] == TOOL_NAME
    assert by_type["TOOL_REQUESTED"]["details"]["arguments"] == {
        "status": "FAILED",
        "limit": 5,
    }

    executed = by_type["TOOL_EXECUTED"]["details"]

    assert executed["tool"] == TOOL_NAME
    assert all(row["status"] == "FAILED" for row in executed["result"])


def test_model_receives_authoritative_rows_not_invented_ones(
    registry,
    seeded_database,
):
    """The tool output fed back to the model must be real database rows."""
    model = FakeQueryModelProvider(arguments='{"status": "RUNNING"}')

    agent = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=AuditStore(database=seeded_database),
    )

    agent.run("Which migrations are still running?")

    assert model.tool_output_seen is not None

    running_batch_ids = [
        spec[0] for spec in DEVELOPMENT_BATCHES if spec[1].value == "RUNNING"
    ]

    for batch_id in running_batch_ids:
        assert f'"batch_id": {batch_id}' in model.tool_output_seen


def test_all_original_tools_are_still_registered(registry):
    names = {schema["name"] for schema in registry.schemas()}

    assert names == {
        "calculator",
        "get_migration_status",
        "restart_migration",
        TOOL_NAME,
    }


def test_restart_migration_remains_a_write_tool(registry):
    assert registry._tools["restart_migration"].risk is ToolRisk.WRITE
