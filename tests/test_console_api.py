"""Read-only endpoints the console depends on: /overview, /tools, audit filters."""

import importlib

import pytest
from fastapi.testclient import TestClient

from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.overview import OverviewService
from app.run_store import RunStore
from tests.fakes import ScriptedModelProvider, final_response, tool_response

QUERY_TOOL = "query_migration_batches"
RESTART_TOOL = "restart_migration"


# -- tool metadata ---------------------------------------------------------


def test_registry_describes_tools_with_risk(registry):
    described = {tool["name"]: tool for tool in registry.describe()}

    assert set(described) == {
        "calculator",
        "get_migration_status",
        RESTART_TOOL,
        QUERY_TOOL,
    }
    assert described[QUERY_TOOL]["risk"] == "READ"
    assert described[RESTART_TOOL]["risk"] == "WRITE"
    assert described[QUERY_TOOL]["parameters"]["additionalProperties"] is False


def test_describe_never_exposes_the_callable(registry):
    for tool in registry.describe():
        assert set(tool) == {"name", "description", "risk", "parameters"}

        for value in tool.values():
            assert not callable(value)


def test_definitions_still_hide_risk_from_the_model(registry):
    """The model is told what a tool does, never how it is governed."""
    for definition in registry.definitions():
        assert not hasattr(definition, "risk")
        assert "risk" not in definition.to_dict()


# -- overview --------------------------------------------------------------


@pytest.fixture
def overview(seeded_database) -> OverviewService:
    return OverviewService(
        run_store=RunStore(database=seeded_database),
        approval_store=ApprovalStore(database=seeded_database),
        audit_store=AuditStore(database=seeded_database),
    )


def test_overview_of_an_empty_system(overview):
    summary = overview.build()

    assert summary["runs_today"] == 0
    assert summary["runs_total"] == 0
    assert summary["pending_approvals"] == 0
    assert summary["tool_executions"] == 0
    assert summary["recent_runs"] == []
    assert summary["recent_events"] == []

    # Every status present as a zero rather than missing, so the UI never gaps.
    assert summary["runs_by_status"] == {
        "RUNNING": 0,
        "WAITING_FOR_APPROVAL": 0,
        "COMPLETED": 0,
        "FAILED": 0,
        "CANCELLED": 0,
    }
    assert summary["approvals_by_status"] == {
        "PENDING": 0,
        "APPROVED": 0,
        "REJECTED": 0,
    }


def test_overview_counts_a_completed_run(agent_factory, overview):
    agent_factory(
        ScriptedModelProvider(
            [tool_response(QUERY_TOOL, {"limit": 1}), final_response("Done.")]
        )
    ).run("Show a batch.")

    summary = overview.build()

    assert summary["runs_total"] == 1
    assert summary["runs_today"] == 1
    assert summary["runs_by_status"]["COMPLETED"] == 1
    assert summary["tool_executions"] == 1
    assert summary["tool_failures"] == 0
    assert len(summary["recent_runs"]) == 1
    assert summary["recent_events"]


def test_overview_counts_a_waiting_run_and_pending_approval(agent_factory, overview):
    agent_factory(
        ScriptedModelProvider([tool_response(RESTART_TOOL, {"batch_id": 43})])
    ).run("Restart batch 43.")

    summary = overview.build()

    assert summary["runs_by_status"]["WAITING_FOR_APPROVAL"] == 1
    assert summary["pending_approvals"] == 1
    assert summary["approvals_by_status"]["PENDING"] == 1


def test_overview_counts_tool_failures(agent_factory, overview):
    agent_factory(
        ScriptedModelProvider(
            [
                tool_response(QUERY_TOOL, {"status": "BROKEN"}),
                final_response("Could not answer."),
            ]
        )
    ).run("Show broken batches.")

    summary = overview.build()

    assert summary["tool_failures"] == 1
    assert summary["events_by_type"]["TOOL_FAILED"] == 1


def test_runs_today_ignores_other_days(agent_factory, overview):
    agent_factory(ScriptedModelProvider([final_response("Hi.")])).run("Hello.")

    assert overview.build()["runs_today"] == 1
    assert overview.build(today="1999-01-01")["runs_today"] == 0


def test_recent_runs_are_capped(agent_factory, overview):
    for index in range(7):
        agent_factory(ScriptedModelProvider([final_response("Hi.")])).run(
            f"Run {index}.",
        )

    summary = overview.build()

    assert summary["runs_total"] == 7
    assert len(summary["recent_runs"]) == 5


# -- endpoints -------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(
        "AGENTOPS_DATABASE_URL",
        f"sqlite:///{tmp_path / 'console_api.db'}",
    )

    import app.main

    module = importlib.reload(app.main)

    module.database.create_all()

    return TestClient(module.app), module


def test_tools_endpoint(client):
    http, _ = client

    response = http.get("/tools")

    assert response.status_code == 200

    tools = {tool["name"]: tool for tool in response.json()}

    assert tools[RESTART_TOOL]["risk"] == "WRITE"
    assert tools[QUERY_TOOL]["risk"] == "READ"
    assert set(tools[QUERY_TOOL]) == {"name", "description", "risk", "parameters"}


def test_overview_endpoint(client):
    http, _ = client

    response = http.get("/overview")

    assert response.status_code == 200

    body = response.json()

    assert body["runs_total"] == 0
    assert body["runs_by_status"]["COMPLETED"] == 0
    assert body["recent_runs"] == []


def test_audit_endpoint_filters_by_event_type(client):
    http, module = client

    module.audit_store.record("TOOL_EXECUTED", {"tool": "a"}, run_id="r1")
    module.audit_store.record("TOOL_FAILED", {"tool": "b"}, run_id="r1")
    module.audit_store.record("TOOL_EXECUTED", {"tool": "c"}, run_id="r2")

    assert len(http.get("/audit/events").json()) == 3

    filtered = http.get("/audit/events", params={"event_type": "TOOL_FAILED"}).json()

    assert len(filtered) == 1
    assert filtered[0]["event_type"] == "TOOL_FAILED"

    scoped = http.get(
        "/audit/events",
        params={"run_id": "r1", "event_type": "TOOL_EXECUTED"},
    ).json()

    assert len(scoped) == 1
    assert scoped[0]["details"]["tool"] == "a"


def test_audit_endpoint_bounds_the_limit(client):
    http, module = client

    for index in range(5):
        module.audit_store.record("TOOL_EXECUTED", {"i": index}, run_id="r")

    assert len(http.get("/audit/events", params={"limit": 2}).json()) == 2
    assert http.get("/audit/events", params={"limit": 0}).status_code == 422
    assert http.get("/audit/events", params={"limit": 501}).status_code == 422


def test_cors_allows_the_local_console_only(client):
    http, _ = client

    allowed = http.get("/health", headers={"Origin": "http://localhost:5173"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

    blocked = http.get("/health", headers={"Origin": "https://evil.example.com"})

    assert "access-control-allow-origin" not in blocked.headers
