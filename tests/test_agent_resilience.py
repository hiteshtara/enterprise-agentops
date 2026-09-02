import json

import pytest

from app.agent import (
    AGENT_FAILED_ANSWER,
    DEFAULT_MAX_ITERATIONS,
    INVALID_ARGUMENTS_ERROR,
)
from app.audit_store import AuditStore
from app.run_store import RunStatus
from app.tool_registry import Tool, ToolRegistry, ToolRisk
from app.tools import restart_migration
from tests.fakes import (
    LoopingModelProvider,
    ScriptedModelProvider,
    final_response,
    tool_response,
)

TOOL_NAME = "query_migration_batches"


def exploding_tool() -> None:
    raise RuntimeError("psycopg2.OperationalError: connection pool exhausted")


def event_types(audit_store, run_id=None):
    return [event["event_type"] for event in audit_store.list_events(run_id=run_id)]


def exploding_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="exploding_tool",
            description="Always fails.",
            function=exploding_tool,
            parameters={},
        )
    )

    return registry


# --------------------------------------------------------------------------
# Model self-correction
# --------------------------------------------------------------------------


def test_model_corrects_invalid_arguments_and_succeeds(agent_factory, seeded_database):
    """Invalid status -> rejected -> model retries with a valid status."""
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, {"status": "BROKEN"}, call_id="attempt-1"),
            tool_response(TOOL_NAME, {"status": "FAILED"}, call_id="attempt-2"),
            final_response("Five batches failed."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = agent_factory(model).run("What migration batches failed?")

    assert result["answer"] == "Five batches failed."
    assert result["approval_required"] is None
    assert result["status"] == RunStatus.COMPLETED.value
    assert model.call_count == 3

    types = event_types(audit_store)

    assert types.count("TOOL_REQUESTED") == 2
    assert types.count("TOOL_FAILED") == 1
    assert types.count("TOOL_EXECUTED") == 1

    # The trace carries only the successful execution.
    assert len(result["trace"]) == 1
    assert result["trace"][0]["arguments"] == {"status": "FAILED"}
    assert all(row["status"] == "FAILED" for row in result["trace"][0]["result"])


def test_failure_is_reported_to_the_model_without_a_traceback(agent_factory):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, {"status": "BROKEN"}),
            final_response("Sorry, I could not answer."),
        ]
    )

    agent_factory(model).run("Show broken batches.")

    seen = model.tool_results_seen()

    assert seen

    payload = json.loads(seen[0])

    assert payload["error"]["type"] == "ValueError"
    assert "Unsupported status" in payload["error"]["message"]
    assert "SUCCESS, FAILED, RUNNING, PENDING" in payload["error"]["message"]

    for marker in ("Traceback", 'File "', "app/migration_store.py", "line "):
        assert marker not in seen[0]


def test_tool_failed_audit_event_details(agent_factory, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, {"limit": 9999}),
            final_response("Could not answer."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    agent_factory(model).run("Show me everything.")

    failed = next(
        event
        for event in audit_store.list_events()
        if event["event_type"] == "TOOL_FAILED"
    )

    assert failed["details"]["tool"] == TOOL_NAME
    assert failed["details"]["arguments"] == {"limit": 9999}
    assert failed["details"]["error_type"] == "ValueError"
    assert "between 1 and 100" in failed["details"]["error"]


def test_type_error_from_a_tool_is_recoverable(agent_factory, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, {"limit": "twenty"}),
            tool_response(TOOL_NAME, {"limit": 2}),
            final_response("Two batches."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = agent_factory(model).run("Show me two batches.")

    assert result["answer"] == "Two batches."

    failed = next(
        event
        for event in audit_store.list_events()
        if event["event_type"] == "TOOL_FAILED"
    )

    assert failed["details"]["error_type"] == "TypeError"


def test_unknown_tool_name_is_recoverable(agent_factory, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response("list_everything", {}),
            tool_response(TOOL_NAME, {"limit": 1}),
            final_response("One batch."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = agent_factory(model).run("List everything.")

    assert result["answer"] == "One batch."
    assert event_types(audit_store).count("TOOL_FAILED") == 1


def test_malformed_arguments_are_recoverable(agent_factory, seeded_database):
    """A provider reports unparsable arguments; the runtime lets the model retry."""
    model = ScriptedModelProvider(
        [
            tool_response(
                TOOL_NAME,
                {},
                argument_error="Tool arguments were not valid JSON: line 1",
            ),
            tool_response(TOOL_NAME, {"limit": 1}),
            final_response("Recovered."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = agent_factory(model).run("Show me a batch.")

    assert result["answer"] == "Recovered."

    types = event_types(audit_store)

    # Arguments never parsed, so no TOOL_REQUESTED for the malformed attempt.
    assert types.count("TOOL_FAILED") == 1
    assert types.count("TOOL_REQUESTED") == 1

    failed = next(
        event
        for event in audit_store.list_events()
        if event["event_type"] == "TOOL_FAILED"
    )

    assert failed["details"]["arguments"] is None
    assert failed["details"]["error_type"] == INVALID_ARGUMENTS_ERROR


# --------------------------------------------------------------------------
# Max iteration guard
# --------------------------------------------------------------------------


def test_max_iterations_stops_the_loop(agent_factory):
    model = LoopingModelProvider()

    result = agent_factory(model, max_iterations=3).run("Loop forever.")

    assert model.call_count == 3
    assert len(result["trace"]) == 3
    assert result["approval_required"] is None
    assert result["status"] == RunStatus.FAILED.value
    assert "maximum of 3 reasoning iterations" in result["answer"]


def test_max_iterations_is_audited(agent_factory, seeded_database):
    audit_store = AuditStore(database=seeded_database)

    agent_factory(LoopingModelProvider(), max_iterations=2).run("Loop forever.")

    events = audit_store.list_events()

    guard = next(
        event for event in events if event["event_type"] == "AGENT_MAX_ITERATIONS"
    )

    assert guard["details"]["max_iterations"] == 2
    assert guard["details"]["tool_calls"] == 2

    types = [event["event_type"] for event in events]

    assert types.count("AGENT_MAX_ITERATIONS") == 1
    assert types.count("TOOL_EXECUTED") == 2


def test_max_iterations_defaults_to_ten(agent_factory):
    agent = agent_factory(LoopingModelProvider())

    assert agent.max_iterations == DEFAULT_MAX_ITERATIONS == 10


def test_max_iterations_is_injectable(agent_factory):
    model = LoopingModelProvider()

    agent_factory(model, max_iterations=1).run("Loop.")

    assert model.call_count == 1


def test_max_iterations_below_one_is_rejected(agent_factory):
    with pytest.raises(ValueError, match="at least 1"):
        agent_factory(LoopingModelProvider(), max_iterations=0)


def test_an_answer_before_the_limit_returns_normally(agent_factory, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, {"limit": 1}),
            final_response("Done."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = agent_factory(model, max_iterations=5).run("Show me one batch.")

    assert result["answer"] == "Done."
    assert result["status"] == RunStatus.COMPLETED.value
    assert "AGENT_MAX_ITERATIONS" not in event_types(audit_store)


# --------------------------------------------------------------------------
# Non-recoverable failures
# --------------------------------------------------------------------------


def test_unexpected_tool_exception_terminates_safely(agent_factory, database):
    model = ScriptedModelProvider(
        [
            tool_response("exploding_tool", {}),
            final_response("Never reached."),
        ]
    )

    audit_store = AuditStore(database=database)

    result = agent_factory(model, tool_registry=exploding_registry()).run("Explode.")

    # The run stops immediately; the model is never asked to retry.
    assert model.call_count == 1
    assert result["answer"] == AGENT_FAILED_ANSWER
    assert result["trace"] == []
    assert result["approval_required"] is None
    assert result["status"] == RunStatus.FAILED.value

    types = event_types(audit_store)

    assert types.count("AGENT_FAILED") == 1
    assert "TOOL_FAILED" not in types
    assert "TOOL_EXECUTED" not in types


def test_unexpected_exception_detail_is_audited_but_not_returned(
    agent_factory,
    database,
):
    audit_store = AuditStore(database=database)

    model = ScriptedModelProvider([tool_response("exploding_tool", {})])

    result = agent_factory(model, tool_registry=exploding_registry()).run("Explode.")

    failure = next(
        event
        for event in audit_store.list_events()
        if event["event_type"] == "AGENT_FAILED"
    )

    assert failure["details"]["error_type"] == "RuntimeError"
    assert "connection pool exhausted" in failure["details"]["error"]

    # The internal message reaches the audit log only.
    assert "connection pool exhausted" not in result["answer"]
    assert "psycopg2" not in result["answer"]
    assert model.tool_results_seen() == []


# --------------------------------------------------------------------------
# Approval semantics are unchanged
# --------------------------------------------------------------------------


def test_approval_required_is_not_a_tool_failure(agent_factory, database):
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

    model = ScriptedModelProvider(
        [
            tool_response("restart_migration", {"batch_id": 43}),
            final_response("Never reached."),
        ]
    )

    audit_store = AuditStore(database=database)

    result = agent_factory(model, tool_registry=registry).run(
        "Restart migration batch 43.",
    )

    assert result["answer"] == "Approval required before executing restart_migration."
    assert result["trace"] == []
    assert result["status"] == RunStatus.WAITING_FOR_APPROVAL.value

    approval = result["approval_required"]

    assert approval is not None
    assert approval["tool"] == "restart_migration"
    assert approval["risk"] == "WRITE"

    types = event_types(audit_store)

    assert types.count("APPROVAL_REQUIRED") == 1
    assert "TOOL_FAILED" not in types
    assert "AGENT_FAILED" not in types
    assert "AGENT_MAX_ITERATIONS" not in types

    # The loop stopped at the approval; the model was called exactly once.
    assert model.call_count == 1
