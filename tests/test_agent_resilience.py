import json
from types import SimpleNamespace

import pytest

from app.agent import (
    AGENT_FAILED_ANSWER,
    DEFAULT_MAX_ITERATIONS,
    AgentService,
)
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.model_provider import ModelProvider
from app.tool_registry import Tool, ToolRegistry, ToolRisk
from app.tools import restart_migration

TOOL_NAME = "query_migration_batches"


def function_call(name: str, arguments: str, call_id: str = "call-1"):
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments=arguments,
        call_id=call_id,
    )


def tool_response(name: str, arguments: str, call_id: str = "call-1"):
    return SimpleNamespace(
        output=[function_call(name, arguments, call_id)],
        output_text="",
    )


def final_response(text: str):
    return SimpleNamespace(output=[], output_text=text)


class ScriptedModelProvider(ModelProvider):
    """Replays a fixed list of responses and records what it was fed."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.call_count = 0
        self.tool_outputs: list[str] = []

    def generate(self, message: str) -> str:
        raise AssertionError("generate() must not be used by the agent loop.")

    def generate_with_tools(self, input_items, tools):
        for item in input_items:
            is_output = (
                isinstance(item, dict) and item.get("type") == "function_call_output"
            )

            if is_output and item["output"] not in self.tool_outputs:
                self.tool_outputs.append(item["output"])

        index = min(self.call_count, len(self.responses) - 1)

        self.call_count += 1

        return self.responses[index]


class LoopingModelProvider(ModelProvider):
    """Always asks for the same valid tool call, never producing an answer."""

    def __init__(self) -> None:
        self.call_count = 0

    def generate(self, message: str) -> str:
        raise AssertionError("generate() must not be used by the agent loop.")

    def generate_with_tools(self, input_items, tools):
        self.call_count += 1

        return tool_response(
            TOOL_NAME,
            '{"limit": 1}',
            call_id=f"loop-{self.call_count}",
        )


def exploding_tool() -> None:
    raise RuntimeError("psycopg2.OperationalError: connection pool exhausted")


def build_agent(registry, database, model, max_iterations=DEFAULT_MAX_ITERATIONS):
    return AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=database),
        audit_store=AuditStore(database=database),
        max_iterations=max_iterations,
    )


def event_types(audit_store):
    return [event["event_type"] for event in audit_store.list_events()]


# --------------------------------------------------------------------------
# Model self-correction
# --------------------------------------------------------------------------


def test_model_corrects_invalid_arguments_and_succeeds(registry, seeded_database):
    """Invalid status -> rejected -> model retries with a valid status."""
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, '{"status": "BROKEN"}', call_id="attempt-1"),
            tool_response(TOOL_NAME, '{"status": "FAILED"}', call_id="attempt-2"),
            final_response("Five batches failed."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    agent = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
    )

    result = agent.run("What migration batches failed?")

    assert result["answer"] == "Five batches failed."
    assert result["approval_required"] is None
    assert model.call_count == 3

    # Two attempts requested, one failed, one executed.
    types = event_types(audit_store)

    assert types.count("TOOL_REQUESTED") == 2
    assert types.count("TOOL_FAILED") == 1
    assert types.count("TOOL_EXECUTED") == 1

    # The trace carries only the successful execution.
    assert len(result["trace"]) == 1
    assert result["trace"][0]["arguments"] == {"status": "FAILED"}
    assert all(row["status"] == "FAILED" for row in result["trace"][0]["result"])


def test_failure_is_reported_to_the_model_without_a_traceback(
    registry,
    seeded_database,
):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, '{"status": "BROKEN"}'),
            final_response("Sorry, I could not answer."),
        ]
    )

    build_agent(registry, seeded_database, model).run("Show broken batches.")

    assert model.tool_outputs

    payload = json.loads(model.tool_outputs[0])

    assert payload["error"]["type"] == "ValueError"
    assert "Unsupported status" in payload["error"]["message"]
    assert "SUCCESS, FAILED, RUNNING, PENDING" in payload["error"]["message"]

    # No traceback or source detail leaks to the model.
    for marker in ("Traceback", 'File "', "app/migration_store.py", "line "):
        assert marker not in model.tool_outputs[0]


def test_tool_failed_audit_event_details(registry, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, '{"limit": 9999}'),
            final_response("Could not answer."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
    ).run("Show me everything.")

    failed = next(
        event
        for event in audit_store.list_events()
        if event["event_type"] == "TOOL_FAILED"
    )

    assert failed["details"]["tool"] == TOOL_NAME
    assert failed["details"]["arguments"] == {"limit": 9999}
    assert failed["details"]["error_type"] == "ValueError"
    assert "between 1 and 100" in failed["details"]["error"]


def test_type_error_from_a_tool_is_recoverable(registry, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, '{"limit": "twenty"}'),
            tool_response(TOOL_NAME, '{"limit": 2}'),
            final_response("Two batches."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
    ).run("Show me two batches.")

    assert result["answer"] == "Two batches."

    failed = next(
        event
        for event in audit_store.list_events()
        if event["event_type"] == "TOOL_FAILED"
    )

    assert failed["details"]["error_type"] == "TypeError"


def test_unknown_tool_name_is_recoverable(registry, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response("list_everything", "{}"),
            tool_response(TOOL_NAME, '{"limit": 1}'),
            final_response("One batch."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
    ).run("List everything.")

    assert result["answer"] == "One batch."
    assert event_types(audit_store).count("TOOL_FAILED") == 1


def test_malformed_json_arguments_are_recoverable(registry, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, "{not json"),
            tool_response(TOOL_NAME, '{"limit": 1}'),
            final_response("Recovered."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
    ).run("Show me a batch.")

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
    assert failed["details"]["error_type"] == "JSONDecodeError"


# --------------------------------------------------------------------------
# Max iteration guard
# --------------------------------------------------------------------------


def test_max_iterations_stops_the_loop(registry, seeded_database):
    model = LoopingModelProvider()

    result = build_agent(registry, seeded_database, model, max_iterations=3).run(
        "Loop forever.",
    )

    assert model.call_count == 3
    assert len(result["trace"]) == 3
    assert result["approval_required"] is None
    assert "maximum of 3 reasoning iterations" in result["answer"]


def test_max_iterations_is_audited(registry, seeded_database):
    audit_store = AuditStore(database=seeded_database)

    AgentService(
        model=LoopingModelProvider(),
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
        max_iterations=2,
    ).run("Loop forever.")

    events = audit_store.list_events()

    guard = next(
        event for event in events if event["event_type"] == "AGENT_MAX_ITERATIONS"
    )

    assert guard["details"]["max_iterations"] == 2
    assert guard["details"]["tool_calls"] == 2

    # Exactly one guard event, and no extra tool ran after the limit.
    types = [event["event_type"] for event in events]

    assert types.count("AGENT_MAX_ITERATIONS") == 1
    assert types.count("TOOL_EXECUTED") == 2


def test_max_iterations_defaults_to_ten(registry, seeded_database):
    agent = AgentService(
        model=LoopingModelProvider(),
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=AuditStore(database=seeded_database),
    )

    assert agent.max_iterations == DEFAULT_MAX_ITERATIONS == 10


def test_max_iterations_is_injectable(registry, seeded_database):
    model = LoopingModelProvider()

    build_agent(registry, seeded_database, model, max_iterations=1).run("Loop.")

    assert model.call_count == 1


def test_max_iterations_below_one_is_rejected(registry, seeded_database):
    with pytest.raises(ValueError, match="at least 1"):
        build_agent(
            registry,
            seeded_database,
            LoopingModelProvider(),
            max_iterations=0,
        )


def test_an_answer_before_the_limit_returns_normally(registry, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(TOOL_NAME, '{"limit": 1}'),
            final_response("Done."),
        ]
    )

    audit_store = AuditStore(database=seeded_database)

    result = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=audit_store,
        max_iterations=5,
    ).run("Show me one batch.")

    assert result["answer"] == "Done."
    assert "AGENT_MAX_ITERATIONS" not in event_types(audit_store)


# --------------------------------------------------------------------------
# Non-recoverable failures
# --------------------------------------------------------------------------


def test_unexpected_tool_exception_terminates_safely(database):
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="exploding_tool",
            description="Always fails.",
            function=exploding_tool,
            parameters={},
        )
    )

    model = ScriptedModelProvider(
        [
            tool_response("exploding_tool", "{}"),
            final_response("Never reached."),
        ]
    )

    audit_store = AuditStore(database=database)

    result = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=database),
        audit_store=audit_store,
    ).run("Explode.")

    # The run stops immediately; the model is never asked to retry.
    assert model.call_count == 1
    assert result["answer"] == AGENT_FAILED_ANSWER
    assert result["trace"] == []
    assert result["approval_required"] is None

    types = event_types(audit_store)

    assert types.count("AGENT_FAILED") == 1
    assert "TOOL_FAILED" not in types
    assert "TOOL_EXECUTED" not in types


def test_unexpected_exception_detail_is_audited_but_not_returned(database):
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="exploding_tool",
            description="Always fails.",
            function=exploding_tool,
            parameters={},
        )
    )

    audit_store = AuditStore(database=database)

    model = ScriptedModelProvider([tool_response("exploding_tool", "{}")])

    result = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=database),
        audit_store=audit_store,
    ).run("Explode.")

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
    assert model.tool_outputs == []


# --------------------------------------------------------------------------
# Approval semantics are unchanged
# --------------------------------------------------------------------------


def test_approval_required_is_not_a_tool_failure(database):
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
            tool_response("restart_migration", '{"batch_id": 43}'),
            final_response("Never reached."),
        ]
    )

    audit_store = AuditStore(database=database)

    result = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=database),
        audit_store=audit_store,
    ).run("Restart migration batch 43.")

    assert result["answer"] == "Approval required before executing restart_migration."
    assert result["trace"] == []

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
