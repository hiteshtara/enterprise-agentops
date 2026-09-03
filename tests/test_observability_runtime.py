"""Metrics captured by the running agent, end to end, and the metrics API."""

import pytest

from app.observability_store import RunMetricsService
from app.run_store import RunStatus
from tests.fakes import (
    ScriptedModelProvider,
    fake_usage,
    final_response,
    tool_response,
)

QUERY_TOOL = "query_migration_batches"
RESTART_TOOL = "restart_migration"


class SteppingClock:
    """Advances a fixed amount on every reading, so durations are exact."""

    def __init__(self, step_ms: int = 10) -> None:
        self.value = 0
        self.step_ns = step_ms * 1_000_000

    def __call__(self) -> int:
        current = self.value
        self.value += self.step_ns

        return current


def metrics_for(database, run_id: str) -> dict:
    return RunMetricsService(database=database).build(run_id)


# -- the loop records what it measured -------------------------------------


def test_a_read_only_run_records_model_and_tool_executions(
    agent_factory, seeded_database
):
    model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"limit": 1}, usage=fake_usage(100, 20)),
            final_response("Done.", usage=fake_usage(150, 30)),
        ]
    )

    result = agent_factory(model, monotonic_ns=SteppingClock(10)).run("Show a batch.")

    metrics = metrics_for(seeded_database, result["run_id"])

    assert metrics["model_calls"] == 2
    assert metrics["tool_calls"] == 1
    assert metrics["tool_failures"] == 0

    # Tokens summed across both model calls.
    assert metrics["input_tokens"] == 250
    assert metrics["output_tokens"] == 50
    assert metrics["total_tokens"] == 300


def test_durations_are_measured_with_the_injected_clock(agent_factory, seeded_database):
    model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"limit": 1}, usage=fake_usage()),
            final_response("Done.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, monotonic_ns=SteppingClock(10)).run("Show a batch.")

    metrics = metrics_for(seeded_database, result["run_id"])

    # Every start/stop pair advances the clock by one 10ms step.
    assert metrics["model_duration_ms"] == 20
    assert metrics["tool_duration_ms"] == 10
    assert metrics["active_execution_ms"] == 30

    for record in metrics["models"]:
        assert record["duration_ms"] == 10
        assert record["started_at"] and record["completed_at"]


def test_the_model_name_is_recorded_per_call(agent_factory, seeded_database):
    model = ScriptedModelProvider([final_response("Hi.", usage=fake_usage())])

    result = agent_factory(model).run("Hello.")

    [record] = metrics_for(seeded_database, result["run_id"])["models"]

    assert record["model"] == "gpt-5.4-mini"
    assert record["provider"] == "openai"
    assert record["status"] == "COMPLETED"


def test_cost_is_estimated_for_a_priced_model(agent_factory, seeded_database):
    model = ScriptedModelProvider([final_response("Hi.", usage=fake_usage(1000, 500))])

    result = agent_factory(model).run("Hello.")

    metrics = metrics_for(seeded_database, result["run_id"])

    assert metrics["estimated_cost_usd"] is not None
    assert metrics["estimated_cost_usd"] > 0


def test_an_unpriced_model_leaves_cost_unknown(agent_factory, seeded_database):
    model = ScriptedModelProvider(
        [final_response("Hi.", usage=fake_usage(), model_name="mystery-model-9")]
    )

    result = agent_factory(model).run("Hello.")

    metrics = metrics_for(seeded_database, result["run_id"])

    assert metrics["estimated_cost_usd"] is None
    # Tokens are still known; only the price is not.
    assert metrics["total_tokens"] == 120


def test_a_provider_reporting_no_usage_leaves_tokens_unknown(
    agent_factory, seeded_database
):
    model = ScriptedModelProvider([final_response("Hi.", usage=None)])

    result = agent_factory(model).run("Hello.")

    metrics = metrics_for(seeded_database, result["run_id"])

    assert metrics["model_calls"] == 1
    assert metrics["total_tokens"] is None
    assert metrics["estimated_cost_usd"] is None


def test_a_failed_tool_is_recorded_and_the_retry_counted(
    agent_factory, seeded_database
):
    """The existing self-correction behaviour, now measured."""
    model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"status": "BROKEN"}, usage=fake_usage()),
            tool_response(QUERY_TOOL, {"status": "FAILED"}, usage=fake_usage()),
            final_response("Recovered.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model).run("What failed?")

    assert result["answer"] == "Recovered."
    assert result["status"] == RunStatus.COMPLETED.value

    metrics = metrics_for(seeded_database, result["run_id"])

    assert metrics["tool_calls"] == 2
    assert metrics["tool_failures"] == 1
    assert metrics["tool_retries"] == 1
    assert metrics["model_calls"] == 3

    failed = [t for t in metrics["tools"] if t["status"] == "FAILED"]

    assert failed[0]["error"]["error_type"] == "ValueError"


def test_a_model_failure_is_recorded_before_it_propagates(
    agent_factory, seeded_database
):
    class ExplodingProvider(ScriptedModelProvider):
        def generate_with_tools(self, messages, tools):
            self.observe(messages, tools)

            raise RuntimeError("provider unreachable")

    agent = agent_factory(ExplodingProvider([]))

    with pytest.raises(RuntimeError, match="provider unreachable"):
        agent.run("Hello.")

    runs = agent.run_store.list_runs(limit=1)

    [record] = metrics_for(seeded_database, runs[0]["run_id"])["models"]

    assert record["status"] == "FAILED"
    assert record["error_type"] == "RuntimeError"
    assert record["total_tokens"] is None


# -- approval boundaries ---------------------------------------------------


def test_a_parked_run_records_no_tool_execution(agent_factory, seeded_database):
    """Blocking on approval is not a tool execution and has no duration."""
    model = ScriptedModelProvider(
        [tool_response(RESTART_TOOL, {"batch_id": 43}, usage=fake_usage())]
    )

    result = agent_factory(model).run("Restart batch 43.")

    assert result["status"] == RunStatus.WAITING_FOR_APPROVAL.value

    metrics = metrics_for(seeded_database, result["run_id"])

    assert metrics["tool_calls"] == 0
    assert metrics["tool_duration_ms"] == 0
    assert metrics["approval_wait_ms"] is None


def test_an_approved_run_measures_the_tool_but_not_the_wait(
    agent_factory, seeded_database
):
    model = ScriptedModelProvider(
        [
            tool_response(RESTART_TOOL, {"batch_id": 43}, usage=fake_usage()),
            final_response("Restarted.", usage=fake_usage()),
        ]
    )

    agent = agent_factory(model, monotonic_ns=SteppingClock(10))

    parked = agent.run("Restart batch 43.")

    agent.resolve_approval(
        approval_id=parked["approval_required"]["approval_id"],
        approved=True,
    )

    metrics = metrics_for(seeded_database, parked["run_id"])

    assert metrics["tool_calls"] == 1
    assert metrics["tool_duration_ms"] == 10
    assert metrics["approval_wait_ms"] is not None
    # Execution time excludes the human wait entirely.
    assert metrics["active_execution_ms"] == metrics["model_duration_ms"] + 10


# -- endpoint --------------------------------------------------------------


def test_metrics_endpoint_returns_measured_values(api):
    module = api.module

    from app.seed_data import seed_migration_batches

    seed_migration_batches(module.database)

    module.agent.model = ScriptedModelProvider(
        [
            tool_response(QUERY_TOOL, {"limit": 1}, usage=fake_usage(100, 20)),
            final_response("Done.", usage=fake_usage(150, 30)),
        ]
    )

    http = api.client("OPERATOR")

    run_id = http.post("/agent/run", json={"message": "Show a batch."}).json()["run_id"]

    response = http.get(f"/runs/{run_id}/metrics")

    assert response.status_code == 200

    body = response.json()

    assert body["run_id"] == run_id
    assert body["model_calls"] == 2
    assert body["tool_calls"] == 1
    assert body["total_tokens"] == 300
    assert len(body["models"]) == 2
    assert len(body["tools"]) == 1


def test_metrics_endpoint_requires_view_runs(api):
    module = api.module

    run_id = module.run_store.create_run("hello")

    assert api.anonymous().get(f"/runs/{run_id}/metrics").status_code == 401

    # Every current role holds VIEW_RUNS, so a viewer may read metrics.
    assert api.client("VIEWER").get(f"/runs/{run_id}/metrics").status_code == 200


def test_metrics_endpoint_404s_for_an_unknown_run(api):
    response = api.client("VIEWER").get("/runs/does-not-exist/metrics")

    assert response.status_code == 404


def test_metrics_endpoint_reports_unknowns_as_null_not_zero(api):
    module = api.module

    run_id = module.run_store.create_run("hello")

    body = api.client("VIEWER").get(f"/runs/{run_id}/metrics").json()

    assert body["total_tokens"] is None
    assert body["estimated_cost_usd"] is None
    assert body["approval_wait_ms"] is None
    assert body["model_calls"] == 0
