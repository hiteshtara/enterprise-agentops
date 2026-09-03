"""Manual reconciliation of runs abandoned by a crashed process."""

from datetime import UTC, datetime, timedelta

import pytest

from app.audit_store import AuditStore
from app.reconciliation import (
    DEFAULT_STALE_AFTER_SECONDS,
    RECONCILED_ANSWER,
    ReconciliationService,
)
from app.run_store import RunStatus, RunStore


def much_later() -> datetime:
    """A moment well past any stale threshold used in these tests."""
    return datetime.now(UTC) + timedelta(hours=1)


@pytest.fixture
def service(database) -> ReconciliationService:
    return ReconciliationService(
        run_store=RunStore(database=database),
        audit_store=AuditStore(database=database),
    )


def test_stale_running_run_is_reconciled(service, run_store):
    run_id = run_store.create_run("Investigate batch 43.")

    reconciled = service.reconcile(now=much_later())

    assert [item["run_id"] for item in reconciled] == [run_id]
    assert reconciled[0]["previous_status"] == RunStatus.RUNNING.value
    assert reconciled[0]["status"] == RunStatus.FAILED.value

    record = run_store.get_run(run_id)

    assert record.status == RunStatus.FAILED.value
    assert record.final_answer == RECONCILED_ANSWER


def test_fresh_running_run_is_untouched(service, run_store):
    run_id = run_store.create_run("Just started.")

    assert service.reconcile() == []
    assert run_store.get_run(run_id).status == RunStatus.RUNNING.value


def test_waiting_for_approval_is_never_reconciled(service, run_store):
    """A run waiting on a human is not stalled, however long it waits."""
    run_id = run_store.create_run("Restart batch 43.")

    run_store.await_approval(run_id, [{"role": "user", "content": "Restart."}])

    assert service.reconcile(now=much_later()) == []
    assert run_store.get_run(run_id).status == (RunStatus.WAITING_FOR_APPROVAL.value)


def test_terminal_runs_are_untouched(service, run_store):
    completed = run_store.create_run("Done.")
    run_store.complete(completed, "All good.")

    failed = run_store.create_run("Broke.")
    run_store.fail(failed, "Something failed.")

    cancelled = run_store.create_run("Rejected.")
    run_store.cancel(cancelled, "Not approved.")

    assert service.reconcile(now=much_later()) == []

    assert run_store.get_run(completed).status == RunStatus.COMPLETED.value
    assert run_store.get_run(completed).final_answer == "All good."
    assert run_store.get_run(failed).status == RunStatus.FAILED.value
    assert run_store.get_run(failed).final_answer == "Something failed."
    assert run_store.get_run(cancelled).status == RunStatus.CANCELLED.value


def test_only_stale_runs_are_reconciled(service, run_store):
    stale = run_store.create_run("Old.")

    reconciled = service.reconcile(now=much_later())

    fresh = run_store.create_run("New.")

    assert [item["run_id"] for item in reconciled] == [stale]
    assert run_store.get_run(fresh).status == RunStatus.RUNNING.value


def test_reconciliation_is_audited_with_run_id(service, run_store, database):
    run_id = run_store.create_run("Investigate batch 43.")

    service.reconcile(now=much_later())

    events = AuditStore(database=database).list_events(run_id=run_id)

    assert [event["event_type"] for event in events] == ["RUN_RECONCILED"]

    details = events[0]["details"]

    assert events[0]["run_id"] == run_id
    assert details["reason"] == RECONCILED_ANSWER
    assert details["previous_status"] == RunStatus.RUNNING.value
    assert details["stale_after_seconds"] == DEFAULT_STALE_AFTER_SECONDS
    assert details["last_updated_at"]


def test_reconciliation_records_a_durable_step(service, run_store):
    run_id = run_store.create_run("Investigate batch 43.")

    service.reconcile(now=much_later())

    steps = run_store.list_steps(run_id)

    assert [step["step_type"] for step in steps] == ["RUN_RECONCILED"]
    assert steps[0]["error"]["reason"] == RECONCILED_ANSWER


def test_reconciling_twice_is_a_no_op(service, run_store, database):
    run_store.create_run("Investigate batch 43.")

    first = service.reconcile(now=much_later())
    second = service.reconcile(now=much_later())

    assert len(first) == 1
    assert second == []

    # The run is FAILED now, so no second audit event is written.
    types = [
        event["event_type"] for event in AuditStore(database=database).list_events()
    ]

    assert types.count("RUN_RECONCILED") == 1


def test_threshold_is_configurable(database, run_store):
    run_id = run_store.create_run("Investigate batch 43.")

    patient = ReconciliationService(
        run_store=run_store,
        audit_store=AuditStore(database=database),
        stale_after_seconds=7200,
    )

    assert patient.reconcile(now=much_later()) == []

    impatient = ReconciliationService(
        run_store=run_store,
        audit_store=AuditStore(database=database),
        stale_after_seconds=1,
    )

    assert [item["run_id"] for item in impatient.reconcile(now=much_later())] == [
        run_id
    ]


def test_invalid_threshold_is_rejected(database, run_store):
    audit_store = AuditStore(database=database)

    with pytest.raises(ValueError, match="stale_after_seconds must be between"):
        ReconciliationService(
            run_store=run_store,
            audit_store=audit_store,
            stale_after_seconds=0,
        )

    with pytest.raises(TypeError, match="must be an integer"):
        ReconciliationService(
            run_store=run_store,
            audit_store=audit_store,
            stale_after_seconds="900",
        )


def test_reconciliation_does_not_touch_the_development_database(
    service,
    run_store,
    development_database_path,
):
    before = (
        development_database_path.stat() if development_database_path.exists() else None
    )

    run_store.create_run("Investigate batch 43.")
    service.reconcile(now=much_later())

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


# -- endpoint --------------------------------------------------------------


def backdate(module, run_id: str, seconds: int = 3600) -> None:
    """Age a run's updated_at so the endpoint's real clock sees it as stale."""
    from app.db_models import RunRecord

    stale_moment = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()

    with module.database.session() as session:
        record = session.get(RunRecord, run_id)
        record.updated_at = stale_moment
        session.commit()


@pytest.fixture
def client(api):
    return api.client("ADMIN"), api.module


def test_endpoint_reconciles_stale_runs(client):
    http, module = client

    run_id = module.run_store.create_run("Investigate batch 43.")

    backdate(module, run_id)

    response = http.post("/runs/reconcile", params={"stale_after_seconds": 1})

    assert response.status_code == 200

    body = response.json()

    assert body["count"] == 1
    assert body["reconciled"][0]["run_id"] == run_id
    assert body["reconciled"][0]["status"] == RunStatus.FAILED.value

    assert module.run_store.get_run(run_id).status == RunStatus.FAILED.value


def test_endpoint_leaves_fresh_runs_alone(client):
    http, module = client

    run_id = module.run_store.create_run("Just started.")

    response = http.post("/runs/reconcile")

    assert response.status_code == 200
    assert response.json() == {"reconciled": [], "count": 0}

    assert module.run_store.get_run(run_id).status == RunStatus.RUNNING.value


def test_endpoint_bounds_the_threshold(client):
    http, _ = client

    assert (
        http.post("/runs/reconcile", params={"stale_after_seconds": 0}).status_code
        == 422
    )
    assert (
        http.post("/runs/reconcile", params={"stale_after_seconds": 86401}).status_code
        == 422
    )
