"""Durable runs, approval resumption, and provider neutrality."""

import json

from app.agent import AgentService
from app.approval_store import ApprovalStatus, ApprovalStore
from app.audit_store import AuditStore
from app.migration_store import MigrationBatchStore
from app.protocol import ModelMessage, ModelResponse, ToolCall, ToolDefinition
from app.run_store import RunStatus, RunStore
from app.tool_setup import build_tool_registry
from tests.fakes import ScriptedModelProvider, final_response, tool_response

QUERY_TOOL = "query_migration_batches"
RESTART_TOOL = "restart_migration"


def investigate_and_restart() -> ScriptedModelProvider:
    """The demo script: read authoritative data, then request a WRITE action."""
    return ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"status": "FAILED", "limit": 5}, "call-read"),
            tool_response(RESTART_TOOL, {"batch_id": 43}, "call-write"),
            final_response(
                "Batch 43 failed because of an Oracle connection timeout. "
                "The approved restart was executed successfully.",
            ),
        ]
    )


# --------------------------------------------------------------------------
# Provider neutrality
# --------------------------------------------------------------------------


def test_agent_hands_the_provider_only_neutral_types(agent_factory):
    model = ScriptedModelProvider([final_response("Hi.")])

    agent_factory(model).run("Hello.")

    conversation = model.conversations[0]

    assert all(isinstance(message, ModelMessage) for message in conversation)
    assert conversation[0].content == "Hello."

    definitions = model.tool_definitions[0]

    assert definitions
    assert all(isinstance(item, ToolDefinition) for item in definitions)


def test_agent_never_sees_openai_shapes(agent_factory):
    """A provider returning only neutral types is sufficient to drive the loop."""
    model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"limit": 1}),
            final_response("Done."),
        ]
    )

    result = agent_factory(model).run("Show a batch.")

    assert result["status"] == RunStatus.COMPLETED.value

    # The tool result the model saw is a JSON string, not a Python object.
    seen = model.tool_results_seen()

    assert json.loads(seen[0])


def test_openai_provider_translates_tool_definitions():
    from app.model_provider import OpenAIModelProvider

    provider = OpenAIModelProvider()

    definitions = [
        ToolDefinition(
            name="query_migration_batches",
            description="Query batches.",
            parameters={"type": "object", "properties": {}},
        )
    ]

    translated = provider.to_openai_tools(definitions)

    assert translated == [
        {
            "type": "function",
            "name": "query_migration_batches",
            "description": "Query batches.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_openai_provider_translates_conversation():
    from app.model_provider import OpenAIModelProvider

    provider = OpenAIModelProvider()

    call = ToolCall(id="c1", name="t", arguments={"a": 1})

    messages = [
        ModelMessage.user("hello"),
        ModelMessage.assistant(None, [call]),
        ModelMessage.tool_result("c1", "t", '{"ok": true}'),
    ]

    items = provider.to_openai_input(messages)

    assert items[0] == {"role": "user", "content": "hello"}
    assert items[1] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "t",
        "arguments": '{"a": 1}',
    }
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "c1",
        "output": '{"ok": true}',
    }


def test_openai_provider_parses_responses_into_neutral_types():
    from types import SimpleNamespace

    from app.model_provider import OpenAIModelProvider

    provider = OpenAIModelProvider()

    raw = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                call_id="c1",
                name="t",
                arguments='{"a": 1}',
            )
        ],
        output_text="",
    )

    response = provider.from_openai_response(raw)

    assert isinstance(response, ModelResponse)
    assert response.text is None
    assert response.tool_calls[0] == ToolCall(id="c1", name="t", arguments={"a": 1})


def test_openai_provider_represents_malformed_arguments():
    from types import SimpleNamespace

    from app.model_provider import OpenAIModelProvider

    provider = OpenAIModelProvider()

    raw = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                call_id="c1",
                name="t",
                arguments="{not json",
            )
        ],
        output_text="",
    )

    call = provider.from_openai_response(raw).tool_calls[0]

    assert call.arguments == {}
    assert "not valid JSON" in call.argument_error


# --------------------------------------------------------------------------
# Run lifecycle
# --------------------------------------------------------------------------


def test_run_is_created_for_every_request(agent_factory, run_store):
    model = ScriptedModelProvider([final_response("Hi.")])

    result = agent_factory(model).run("Hello.")

    record = run_store.get_run(result["run_id"])

    assert record is not None
    assert record.user_message == "Hello."


def test_read_only_run_completes(agent_factory, run_store):
    model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"status": "FAILED", "limit": 3}),
            final_response("Three batches failed."),
        ]
    )

    result = agent_factory(model).run("What failed?")

    record = run_store.get_run(result["run_id"])

    assert record.status == RunStatus.COMPLETED.value
    assert record.final_answer == "Three batches failed."


def test_run_steps_are_persisted(agent_factory, run_store):
    model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"limit": 1}),
            final_response("Done."),
        ]
    )

    result = agent_factory(model).run("Show a batch.")

    types = [step["step_type"] for step in run_store.list_steps(result["run_id"])]

    assert types == [
        "MODEL_RESPONSE",
        "TOOL_REQUESTED",
        "TOOL_EXECUTED",
        "MODEL_RESPONSE",
    ]


def test_write_request_parks_the_run(agent_factory, run_store):
    model = ScriptedModelProvider(
        [
            tool_response(RESTART_TOOL, {"batch_id": 43}),
            final_response("Never reached."),
        ]
    )

    result = agent_factory(model).run("Restart batch 43.")

    assert result["status"] == RunStatus.WAITING_FOR_APPROVAL.value
    assert run_store.get_run(result["run_id"]).status == (
        RunStatus.WAITING_FOR_APPROVAL.value
    )


def test_approval_is_linked_to_the_run(agent_factory, seeded_database):
    model = ScriptedModelProvider([tool_response(RESTART_TOOL, {"batch_id": 43})])

    result = agent_factory(model).run("Restart batch 43.")

    approval_id = result["approval_required"]["approval_id"]

    record = ApprovalStore(database=seeded_database).get(approval_id)

    assert record.run_id == result["run_id"]
    assert record.tool_call_id == "call-1"
    assert record.status == ApprovalStatus.PENDING.value


# --------------------------------------------------------------------------
# Approval resumption
# --------------------------------------------------------------------------


def test_approved_action_resumes_the_original_run(agent_factory, run_store):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate migration batch 43 and restart it if needed.")

    assert parked["status"] == RunStatus.WAITING_FOR_APPROVAL.value
    assert model.call_count == 2

    resumed = agent.resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=True,
    )

    # Same run, no need to resend the prompt.
    assert resumed["run_id"] == parked["run_id"]
    assert resumed["approved"] is True
    assert resumed["run_status"] == RunStatus.COMPLETED.value
    assert "restart was executed successfully" in resumed["answer"]

    # The model was called a third time to produce the final answer.
    assert model.call_count == 3

    record = run_store.get_run(parked["run_id"])

    assert record.status == RunStatus.COMPLETED.value
    assert record.final_answer == resumed["answer"]


def test_tool_result_is_returned_to_the_model_after_approval(agent_factory):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    agent.resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=True,
    )

    # The final model call saw the restart result appended to the conversation.
    final_conversation = model.conversations[-1]

    tool_messages = [
        message for message in final_conversation if message.role.value == "tool"
    ]

    assert len(tool_messages) == 2
    assert tool_messages[1].tool_name == RESTART_TOOL
    assert json.loads(tool_messages[1].content)["status"] == "RESTARTED"

    # The original user prompt survived the approval wait.
    assert final_conversation[0].content == "Investigate and restart batch 43."


def test_resumed_run_records_the_full_step_history(agent_factory, run_store):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    agent.resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=True,
    )

    types = [step["step_type"] for step in run_store.list_steps(parked["run_id"])]

    assert types == [
        "MODEL_RESPONSE",
        "TOOL_REQUESTED",
        "TOOL_EXECUTED",
        "MODEL_RESPONSE",
        "TOOL_REQUESTED",
        "APPROVAL_REQUIRED",
        "APPROVAL_GRANTED",
        "TOOL_EXECUTED",
        "MODEL_RESPONSE",
    ]


def test_approval_remains_queryable_after_approval(agent_factory, seeded_database):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    approval_id = parked["approval_required"]["approval_id"]

    agent.resolve_approval(approval_id=approval_id, approved=True)

    store = ApprovalStore(database=seeded_database)

    record = store.get(approval_id)

    assert record is not None
    assert record.status == ApprovalStatus.APPROVED.value
    assert record.resolved_at is not None

    approved = store.list_approvals(status=ApprovalStatus.APPROVED.value)

    assert [item["approval_id"] for item in approved] == [approval_id]


# --------------------------------------------------------------------------
# Rejection
# --------------------------------------------------------------------------


def test_rejection_does_not_execute_the_tool(agent_factory, seeded_database):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    before = model.call_count

    rejected = agent.resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=False,
    )

    assert rejected["approved"] is False
    assert rejected["result"] is None
    assert rejected["trace"] == []

    # The model is not consulted again; the run simply ends.
    assert model.call_count == before

    executed = [
        event
        for event in AuditStore(database=seeded_database).list_events()
        if event["event_type"] == "TOOL_EXECUTED"
    ]

    assert all(event["details"]["tool"] != RESTART_TOOL for event in executed)


def test_rejected_run_reaches_a_terminal_status(agent_factory, run_store):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    rejected = agent.resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=False,
    )

    assert rejected["run_status"] == RunStatus.CANCELLED.value
    assert run_store.get_run(parked["run_id"]).status == RunStatus.CANCELLED.value


def test_rejected_approval_remains_in_history(agent_factory, seeded_database):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    approval_id = parked["approval_required"]["approval_id"]

    agent.resolve_approval(approval_id=approval_id, approved=False)

    record = ApprovalStore(database=seeded_database).get(approval_id)

    assert record.status == ApprovalStatus.REJECTED.value
    assert record.decision == ApprovalStatus.REJECTED.value
    assert record.resolved_at is not None


def test_an_approval_cannot_be_resolved_twice(agent_factory):
    import pytest

    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    approval_id = parked["approval_required"]["approval_id"]

    agent.resolve_approval(approval_id=approval_id, approved=True)

    with pytest.raises(ValueError, match="already resolved"):
        agent.resolve_approval(approval_id=approval_id, approved=True)


# --------------------------------------------------------------------------
# Durability across instances
# --------------------------------------------------------------------------


def test_run_survives_a_second_store_instance(agent_factory, seeded_database):
    model = ScriptedModelProvider([final_response("Hi.")])

    result = agent_factory(model).run("Hello.")

    fresh = RunStore(database=seeded_database)

    assert fresh.get_run(result["run_id"]).user_message == "Hello."


def test_resumption_survives_a_rebuilt_agent(seeded_database):
    """The waiting run is resumed by a different AgentService instance."""
    registry = build_tool_registry(
        migration_store=MigrationBatchStore(database=seeded_database),
    )

    def build(model):
        return AgentService(
            model=model,
            tool_registry=registry,
            approval_store=ApprovalStore(database=seeded_database),
            audit_store=AuditStore(database=seeded_database),
            run_store=RunStore(database=seeded_database),
        )

    parked = build(
        ScriptedModelProvider([tool_response(RESTART_TOOL, {"batch_id": 43})]),
    ).run("Restart batch 43.")

    assert parked["status"] == RunStatus.WAITING_FOR_APPROVAL.value

    # Everything below runs against fresh objects, as a second process would.
    resumed_model = ScriptedModelProvider([final_response("Restarted batch 43.")])

    resumed = build(resumed_model).resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=True,
    )

    assert resumed["run_id"] == parked["run_id"]
    assert resumed["run_status"] == RunStatus.COMPLETED.value
    assert resumed["answer"] == "Restarted batch 43."

    # The rebuilt agent replayed the original prompt from the database.
    assert resumed_model.conversations[0][0].content == "Restart batch 43."


def test_persisted_conversation_is_plain_json(agent_factory, run_store):
    model = ScriptedModelProvider([tool_response(RESTART_TOOL, {"batch_id": 43})])

    result = agent_factory(model).run("Restart batch 43.")

    record = run_store.get_run(result["run_id"])

    # Round-trips through json with no custom encoder: no SDK objects persisted.
    reparsed = json.loads(record.conversation_json)

    assert isinstance(reparsed, list)
    assert reparsed[0] == {
        "role": "user",
        "content": "Restart batch 43.",
        "tool_calls": [],
        "tool_call_id": None,
        "tool_name": None,
    }
    assert reparsed[1]["tool_calls"][0]["name"] == RESTART_TOOL


# --------------------------------------------------------------------------
# Audit run_id
# --------------------------------------------------------------------------


def test_audit_events_carry_run_id(agent_factory, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"limit": 1}),
            final_response("Done."),
        ]
    )

    result = agent_factory(model).run("Show a batch.")

    events = AuditStore(database=seeded_database).list_events()

    assert events
    assert all(event["run_id"] == result["run_id"] for event in events)


def test_audit_events_filter_by_run_id(agent_factory, seeded_database):
    first = agent_factory(
        ScriptedModelProvider(
            [tool_response(QUERY_TOOL, {"limit": 1}), final_response("A.")]
        )
    ).run("First.")

    second = agent_factory(
        ScriptedModelProvider(
            [tool_response(QUERY_TOOL, {"limit": 2}), final_response("B.")]
        )
    ).run("Second.")

    store = AuditStore(database=seeded_database)

    assert len(store.list_events()) == 4

    first_events = store.list_events(run_id=first["run_id"])

    assert len(first_events) == 2
    assert all(event["run_id"] == first["run_id"] for event in first_events)

    assert len(store.list_events(run_id=second["run_id"])) == 2
    assert store.list_events(run_id="no-such-run") == []


def test_approval_audit_events_carry_run_id(agent_factory, seeded_database):
    model = investigate_and_restart()

    agent = agent_factory(model)

    parked = agent.run("Investigate and restart batch 43.")

    agent.resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=True,
    )

    events = AuditStore(database=seeded_database).list_events(
        run_id=parked["run_id"],
    )

    types = [event["event_type"] for event in events]

    assert "APPROVAL_REQUIRED" in types
    assert "APPROVAL_GRANTED" in types
    assert all(event["run_id"] == parked["run_id"] for event in events)
