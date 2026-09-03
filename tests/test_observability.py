"""Observability V1: usage normalisation, measurement, pricing, aggregation."""

from types import SimpleNamespace

import pytest

from app.observability_store import (
    ExecutionStatus,
    ModelExecutionStore,
    RunMetricsService,
    ToolExecutionStore,
    sum_or_none,
)
from app.pricing import ModelPricing, PricingRegistry
from app.protocol import ModelUsage
from app.timing import Stopwatch

MODEL = "gpt-5.4-mini"


class FakeClock:
    """A monotonic source tests advance explicitly, so nothing sleeps."""

    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        return self.value

    def advance_ms(self, ms: int) -> None:
        self.value += ms * 1_000_000


def openai_response(usage=None, model=MODEL, request_id="resp_1"):
    return SimpleNamespace(
        output=[],
        output_text="done",
        usage=usage,
        model=model,
        id=request_id,
    )


def usage_block(
    input_tokens=120,
    output_tokens=45,
    total_tokens=165,
    cached=None,
    reasoning=None,
):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


# -- provider normalisation ------------------------------------------------


def provider():
    from app.model_provider import OpenAIModelProvider

    return OpenAIModelProvider()


def test_usage_is_normalised_into_provider_neutral_fields():
    response = provider().from_openai_response(openai_response(usage_block()))

    assert isinstance(response.usage, ModelUsage)
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 45
    assert response.usage.total_tokens == 165
    assert response.model_name == MODEL
    assert response.provider_request_id == "resp_1"


def test_detail_counters_are_normalised():
    response = provider().from_openai_response(
        openai_response(usage_block(cached=64, reasoning=12))
    )

    assert response.usage.cached_input_tokens == 64
    assert response.usage.reasoning_tokens == 12


def test_missing_usage_yields_none_not_zero():
    response = provider().from_openai_response(openai_response(usage=None))

    assert response.usage is None


def test_absent_counters_stay_none():
    response = provider().from_openai_response(
        openai_response(
            usage_block(input_tokens=None, output_tokens=None, total_tokens=None)
        )
    )

    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None
    assert response.usage.total_tokens is None


def test_total_is_derived_only_from_reported_parts():
    response = provider().from_openai_response(
        openai_response(
            usage_block(input_tokens=100, output_tokens=20, total_tokens=None)
        )
    )

    assert response.usage.total_tokens == 120


def test_non_numeric_counters_are_rejected():
    response = provider().from_openai_response(
        openai_response(usage_block(input_tokens="lots"))
    )

    assert response.usage.input_tokens is None


def test_no_openai_object_escapes_the_provider():
    response = provider().from_openai_response(openai_response(usage_block()))

    assert type(response.usage).__module__ == "app.protocol"


# -- timing ----------------------------------------------------------------


def test_stopwatch_measures_with_the_injected_monotonic_clock():
    clock = FakeClock()

    with Stopwatch(clock) as watch:
        clock.advance_ms(250)

    assert watch.duration_ms == 250
    assert watch.started_at and watch.completed_at


def test_stopwatch_reports_none_before_it_runs():
    assert Stopwatch(FakeClock()).duration_ms is None


# -- pricing ---------------------------------------------------------------


def test_cost_is_estimated_from_reported_tokens():
    registry = PricingRegistry(
        {
            ("openai", MODEL): ModelPricing(
                input_per_million=1.0, output_per_million=10.0
            )
        }
    )

    cost = registry.estimate(
        "openai",
        MODEL,
        ModelUsage(input_tokens=1_000_000, output_tokens=100_000),
    )

    assert cost == pytest.approx(2.0)


def test_cached_input_is_billed_at_its_own_rate():
    registry = PricingRegistry(
        {
            ("openai", MODEL): ModelPricing(
                input_per_million=10.0,
                output_per_million=0.0,
                cached_input_per_million=1.0,
            )
        }
    )

    cost = registry.estimate(
        "openai",
        MODEL,
        ModelUsage(
            input_tokens=1_000_000, cached_input_tokens=500_000, output_tokens=0
        ),
    )

    # Half at full rate, half cached.
    assert cost == pytest.approx(5.5)


def test_unknown_model_has_no_cost_rather_than_zero():
    registry = PricingRegistry({})

    assert (
        registry.estimate("openai", "some-unpriced-model", ModelUsage(input_tokens=100))
        is None
    )


def test_unknown_model_never_borrows_another_models_price():
    registry = PricingRegistry(
        {
            ("openai", MODEL): ModelPricing(
                input_per_million=99.0, output_per_million=99.0
            )
        }
    )

    assert (
        registry.estimate("openai", "different-model", ModelUsage(input_tokens=1))
        is None
    )


def test_no_usage_means_no_cost():
    registry = PricingRegistry(
        {("openai", MODEL): ModelPricing(input_per_million=1.0, output_per_million=1.0)}
    )

    assert registry.estimate("openai", MODEL, None) is None
    assert registry.estimate("openai", MODEL, ModelUsage()) is None


# -- aggregation helpers ---------------------------------------------------


def test_sum_or_none_ignores_unknowns_but_keeps_known_values():
    assert sum_or_none([1, None, 2]) == 3
    assert sum_or_none([None, None]) is None
    assert sum_or_none([]) is None


# -- persistence -----------------------------------------------------------


def test_model_execution_duration_and_usage_persist(database, run_store):
    run_id = run_store.create_run("hello")

    ModelExecutionStore(database=database).record(
        run_id=run_id,
        provider="openai",
        model=MODEL,
        status=ExecutionStatus.COMPLETED,
        started_at="2026-09-03T00:00:00+00:00",
        completed_at="2026-09-03T00:00:01+00:00",
        duration_ms=940,
        usage=ModelUsage(input_tokens=100, output_tokens=20, total_tokens=120),
        estimated_cost_usd=0.000145,
    )

    [record] = ModelExecutionStore(database=database).list_for_run(run_id)

    assert record["duration_ms"] == 940
    assert record["input_tokens"] == 100
    assert record["total_tokens"] == 120
    assert record["estimated_cost_usd"] == pytest.approx(0.000145)
    assert record["status"] == "COMPLETED"


def test_failed_model_execution_is_captured_without_usage(database, run_store):
    run_id = run_store.create_run("hello")

    ModelExecutionStore(database=database).record(
        run_id=run_id,
        provider="openai",
        model=None,
        status=ExecutionStatus.FAILED,
        started_at="2026-09-03T00:00:00+00:00",
        completed_at=None,
        duration_ms=12,
        error_type="APIConnectionError",
        error_message="connection reset",
    )

    [record] = ModelExecutionStore(database=database).list_for_run(run_id)

    assert record["status"] == "FAILED"
    assert record["error_type"] == "APIConnectionError"
    assert record["total_tokens"] is None
    assert record["estimated_cost_usd"] is None


def test_tool_execution_duration_persists(database, run_store):
    run_id = run_store.create_run("hello")

    ToolExecutionStore(database=database).record(
        run_id=run_id,
        tool_name="query_migration_batches",
        status=ExecutionStatus.COMPLETED,
        started_at="2026-09-03T00:00:00+00:00",
        completed_at="2026-09-03T00:00:00.030+00:00",
        duration_ms=30,
        arguments={"limit": 1},
        result=[{"batch_id": 43}],
    )

    [record] = ToolExecutionStore(database=database).list_for_run(run_id)

    assert record["duration_ms"] == 30
    assert record["status"] == "COMPLETED"
    assert record["arguments"] == {"limit": 1}
    assert record["retry_number"] == 0


def test_retry_number_counts_prior_failures_of_the_same_tool(database, run_store):
    run_id = run_store.create_run("hello")

    store = ToolExecutionStore(database=database)

    store.record(
        run_id=run_id,
        tool_name="query_migration_batches",
        status=ExecutionStatus.FAILED,
        started_at="t0",
        completed_at="t0",
        duration_ms=1,
        error={"error_type": "ValueError"},
    )
    store.record(
        run_id=run_id,
        tool_name="query_migration_batches",
        status=ExecutionStatus.COMPLETED,
        started_at="t1",
        completed_at="t1",
        duration_ms=2,
    )
    # A different tool is unaffected by that failure.
    store.record(
        run_id=run_id,
        tool_name="restart_migration",
        status=ExecutionStatus.COMPLETED,
        started_at="t2",
        completed_at="t2",
        duration_ms=3,
    )

    records = {r["tool_name"] + r["status"]: r for r in store.list_for_run(run_id)}

    assert records["query_migration_batchesCOMPLETED"]["retry_number"] == 1
    assert records["restart_migrationCOMPLETED"]["retry_number"] == 0


# -- run metrics -----------------------------------------------------------


def test_metrics_aggregate_tokens_and_cost_across_calls(database, run_store):
    run_id = run_store.create_run("hello")

    store = ModelExecutionStore(database=database)

    for tokens, cost in ((100, 0.001), (250, 0.002)):
        store.record(
            run_id=run_id,
            provider="openai",
            model=MODEL,
            status=ExecutionStatus.COMPLETED,
            started_at="t",
            completed_at="t",
            duration_ms=100,
            usage=ModelUsage(
                input_tokens=tokens, output_tokens=10, total_tokens=tokens + 10
            ),
            estimated_cost_usd=cost,
        )

    metrics = RunMetricsService(database=database).build(run_id)

    assert metrics["model_calls"] == 2
    assert metrics["input_tokens"] == 350
    assert metrics["output_tokens"] == 20
    assert metrics["total_tokens"] == 370
    assert metrics["estimated_cost_usd"] == pytest.approx(0.003)
    assert metrics["model_duration_ms"] == 200


def test_unpriced_calls_leave_cost_unknown(database, run_store):
    run_id = run_store.create_run("hello")

    ModelExecutionStore(database=database).record(
        run_id=run_id,
        provider="openai",
        model="unpriced",
        status=ExecutionStatus.COMPLETED,
        started_at="t",
        completed_at="t",
        duration_ms=10,
        usage=ModelUsage(input_tokens=10),
        estimated_cost_usd=None,
    )

    metrics = RunMetricsService(database=database).build(run_id)

    assert metrics["estimated_cost_usd"] is None
    assert metrics["input_tokens"] == 10


def test_metrics_count_tool_outcomes(database, run_store):
    run_id = run_store.create_run("hello")

    store = ToolExecutionStore(database=database)

    store.record(
        run_id=run_id,
        tool_name="t",
        status=ExecutionStatus.FAILED,
        started_at="a",
        completed_at="a",
        duration_ms=5,
    )
    store.record(
        run_id=run_id,
        tool_name="t",
        status=ExecutionStatus.COMPLETED,
        started_at="b",
        completed_at="b",
        duration_ms=7,
    )

    metrics = RunMetricsService(database=database).build(run_id)

    assert metrics["tool_calls"] == 2
    assert metrics["tool_failures"] == 1
    assert metrics["tool_retries"] == 1
    assert metrics["tool_duration_ms"] == 12


def test_metrics_are_scoped_to_one_run(database, run_store):
    first = run_store.create_run("first")
    second = run_store.create_run("second")

    store = ToolExecutionStore(database=database)

    store.record(
        run_id=first,
        tool_name="t",
        status=ExecutionStatus.COMPLETED,
        started_at="a",
        completed_at="a",
        duration_ms=5,
    )

    assert RunMetricsService(database=database).build(first)["tool_calls"] == 1
    assert RunMetricsService(database=database).build(second)["tool_calls"] == 0


def test_active_execution_excludes_approval_wait(database, run_store):
    """Time a human spent deciding is not execution time."""
    from app.approval_store import ApprovalStore

    run_id = run_store.create_run("hello")

    ToolExecutionStore(database=database).record(
        run_id=run_id,
        tool_name="t",
        status=ExecutionStatus.COMPLETED,
        started_at="a",
        completed_at="a",
        duration_ms=40,
    )

    approvals = ApprovalStore(database=database)

    approval = approvals.create(
        tool="restart_migration",
        arguments={},
        risk="WRITE",
        run_id=run_id,
        tool_call_id="c1",
    )

    approvals.resolve(approval.approval_id, approved=True)

    metrics = RunMetricsService(database=database).build(run_id)

    assert metrics["active_execution_ms"] == 40
    assert metrics["approval_wait_ms"] is not None
    assert metrics["approval_wait_ms"] >= 0


def test_approval_wait_sums_multiple_approvals(database, run_store):
    from app.db_models import ApprovalRecord

    run_id = run_store.create_run("hello")

    with database.session() as session:
        for index, (created, resolved) in enumerate(
            [
                ("2026-09-03T00:00:00+00:00", "2026-09-03T00:00:10+00:00"),
                ("2026-09-03T00:01:00+00:00", "2026-09-03T00:01:05+00:00"),
            ]
        ):
            session.add(
                ApprovalRecord(
                    approval_id=f"a{index}",
                    run_id=run_id,
                    tool_call_id="c",
                    tool="restart_migration",
                    arguments_json="{}",
                    risk="WRITE",
                    status="APPROVED",
                    created_at=created,
                    resolved_at=resolved,
                    decision="APPROVED",
                )
            )
        session.commit()

    # 10s + 5s, summed.
    assert (
        RunMetricsService(database=database).build(run_id)["approval_wait_ms"] == 15_000
    )


def test_pending_approval_contributes_no_wait(database, run_store):
    from app.approval_store import ApprovalStore

    run_id = run_store.create_run("hello")

    ApprovalStore(database=database).create(
        tool="restart_migration",
        arguments={},
        risk="WRITE",
        run_id=run_id,
        tool_call_id="c1",
    )

    # The wait has not finished, so it is unknown rather than zero.
    assert (
        RunMetricsService(database=database).build(run_id)["approval_wait_ms"] is None
    )


def test_a_run_with_no_activity_reports_zero_counts_and_unknown_tokens(
    database, run_store
):
    run_id = run_store.create_run("hello")

    metrics = RunMetricsService(database=database).build(run_id)

    assert metrics["model_calls"] == 0
    assert metrics["tool_calls"] == 0
    assert metrics["total_tokens"] is None
    assert metrics["estimated_cost_usd"] is None


def test_metrics_do_not_write_audit_events(database, run_store):
    """Observability and audit stay separate stores."""
    from app.audit_store import AuditStore

    run_id = run_store.create_run("hello")

    ModelExecutionStore(database=database).record(
        run_id=run_id,
        provider="openai",
        model=MODEL,
        status=ExecutionStatus.COMPLETED,
        started_at="t",
        completed_at="t",
        duration_ms=1,
    )

    assert AuditStore(database=database).list_events(run_id=run_id) == []


def test_metrics_do_not_touch_the_development_database(
    database, run_store, development_database_path
):
    before = (
        development_database_path.stat() if development_database_path.exists() else None
    )

    run_id = run_store.create_run("hello")

    ModelExecutionStore(database=database).record(
        run_id=run_id,
        provider="openai",
        model=MODEL,
        status=ExecutionStatus.COMPLETED,
        started_at="t",
        completed_at="t",
        duration_ms=1,
    )

    after = (
        development_database_path.stat() if development_database_path.exists() else None
    )

    if before is None:
        assert after is None
    else:
        assert (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        )


def test_a_dated_snapshot_is_priced_only_when_listed_explicitly():
    """Snapshot ids are configured entries, never prefix-matched."""
    from app.pricing import DEFAULT_PRICES, PricingRegistry

    registry = PricingRegistry(DEFAULT_PRICES)

    usage = ModelUsage(input_tokens=1000, output_tokens=100)

    # Configured explicitly.
    assert registry.estimate("openai", "gpt-5.4-mini-2026-03-17", usage) is not None

    # An unlisted snapshot of the same family is not priced by resemblance.
    assert registry.estimate("openai", "gpt-5.4-mini-2099-01-01", usage) is None
