import pytest

from app.audit_store import AuditStore
from app.migration_store import (
    ALLOWED_STATUSES,
    MAX_LIMIT,
    MIN_LIMIT,
)
from app.seed_data import DEVELOPMENT_BATCHES
from app.tool_registry import ToolRisk
from tests.fakes import ScriptedModelProvider, final_response, tool_response

TOOL_NAME = "query_migration_batches"


def test_tool_is_registered_as_read(registry):
    schema_names = [definition.name for definition in registry.definitions()]

    assert TOOL_NAME in schema_names

    assert registry._tools[TOOL_NAME].risk is ToolRisk.READ


def test_tool_requires_no_approval(registry):
    # A READ tool executes without approved=True.
    result = registry.execute(TOOL_NAME, {"limit": 3})

    assert len(result) == 3


def test_tool_schema_constrains_status_and_limit(registry):
    definition = next(d for d in registry.definitions() if d.name == TOOL_NAME)

    parameters = definition.parameters

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
    definition = next(d for d in registry.definitions() if d.name == TOOL_NAME)

    properties = definition.parameters["properties"]

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


def failed_query_script():
    return ScriptedModelProvider(
        [
            tool_response(
                TOOL_NAME,
                {"status": "FAILED", "limit": 5},
                call_id="query-call-1",
            ),
            final_response("Batches 43, 46, 49, 53 and 57 failed."),
        ]
    )


def test_agent_executes_the_database_tool(agent_factory):
    model = failed_query_script()

    result = agent_factory(model).run("Show me failed migration batches.")

    assert result["approval_required"] is None
    assert result["answer"] == "Batches 43, 46, 49, 53 and 57 failed."
    assert model.call_count == 2


def test_execution_trace_contains_the_database_tool(agent_factory):
    result = agent_factory(failed_query_script()).run(
        "Show me failed migration batches.",
    )

    assert len(result["trace"]) == 1

    step = result["trace"][0]

    assert step["tool"] == TOOL_NAME
    assert step["arguments"] == {"status": "FAILED", "limit": 5}
    assert all(row["status"] == "FAILED" for row in step["result"])


def test_audit_records_tool_requested_and_executed(agent_factory, seeded_database):

    agent_factory(failed_query_script()).run("Show me failed migration batches.")

    events = AuditStore(database=seeded_database).list_events()

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


def test_model_receives_authoritative_rows_not_invented_ones(agent_factory):
    """The tool output fed back to the model must be real database rows."""
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, {"status": "RUNNING"}),
            final_response("One migration is running."),
        ]
    )

    agent_factory(model).run("Which migrations are still running?")

    seen = model.tool_results_seen()

    assert seen

    running_batch_ids = [
        spec[0] for spec in DEVELOPMENT_BATCHES if spec[1].value == "RUNNING"
    ]

    for batch_id in running_batch_ids:
        assert f'"batch_id": {batch_id}' in seen[0]


def test_all_original_tools_are_still_registered(registry):
    names = {definition.name for definition in registry.definitions()}

    assert names == {
        "calculator",
        "get_migration_status",
        "restart_migration",
        TOOL_NAME,
    }


def test_restart_migration_remains_a_write_tool(registry):
    assert registry._tools["restart_migration"].risk is ToolRisk.WRITE
