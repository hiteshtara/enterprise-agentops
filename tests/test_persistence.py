from pathlib import Path

import pytest

from app.approval_store import ApprovalStatus, ApprovalStore
from app.audit_store import AuditStore
from app.database import DEFAULT_DATABASE_URL, Database
from app.run_store import RunStatus, RunStore


def snapshot(path):
    """Capture enough file state to detect any write to the development database."""
    if not path.exists():
        return None

    stat = path.stat()

    return (stat.st_size, stat.st_mtime_ns)


def make_approval(store: ApprovalStore, run_id: str = "run-1"):
    return store.create(
        tool="restart_migration",
        arguments={"batch_id": 43},
        risk="WRITE",
        run_id=run_id,
        tool_call_id="call-1",
    )


def test_approval_persists_in_configured_database(database):
    created = make_approval(ApprovalStore(database=database))

    # A second store on the same Database reads the row back, proving it was
    # committed rather than held in a single session's identity map.
    reloaded = ApprovalStore(database=database).get(created.approval_id)

    assert reloaded is not None
    assert reloaded.tool == "restart_migration"
    assert reloaded.arguments == {"batch_id": 43}
    assert reloaded.risk == "WRITE"
    assert reloaded.run_id == "run-1"
    assert reloaded.status == ApprovalStatus.PENDING.value


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
    assert AuditStore(database=database).list_events() == []
    assert ApprovalStore(database=database).list_approvals() == []
    assert RunStore(database=database).list_runs() == []


def test_database_fixture_is_isolated_from_development_database(
    database,
    development_database_path,
):
    assert database.url != DEFAULT_DATABASE_URL

    fixture_path = Path(database.url.removeprefix("sqlite:///"))

    assert fixture_path.resolve() != development_database_path.resolve()

    before = snapshot(development_database_path)

    make_approval(ApprovalStore(database=database))
    AuditStore(database=database).record("TOOL_REQUESTED", {"tool": "x"})
    RunStore(database=database).create_run("hello")

    assert snapshot(development_database_path) == before


def test_resolved_approvals_are_kept_not_deleted(database):
    store = ApprovalStore(database=database)

    created = make_approval(store)

    store.resolve(created.approval_id, approved=True)

    reloaded = store.get(created.approval_id)

    assert reloaded is not None
    assert reloaded.status == ApprovalStatus.APPROVED.value
    assert reloaded.decision == ApprovalStatus.APPROVED.value
    assert reloaded.resolved_at is not None


def test_rejected_approvals_are_kept(database):
    store = ApprovalStore(database=database)

    created = make_approval(store)

    store.resolve(created.approval_id, approved=False)

    assert store.get(created.approval_id).status == ApprovalStatus.REJECTED.value

    rejected = store.list_approvals(status=ApprovalStatus.REJECTED.value)

    assert len(rejected) == 1
    assert rejected[0]["approval_id"] == created.approval_id


def test_approvals_filter_by_status_and_run(database):
    store = ApprovalStore(database=database)

    first = make_approval(store, run_id="run-a")
    make_approval(store, run_id="run-b")

    store.resolve(first.approval_id, approved=True)

    assert len(store.list_approvals()) == 2
    assert len(store.list_approvals(status=ApprovalStatus.PENDING.value)) == 1
    assert len(store.list_approvals(run_id="run-a")) == 1


def test_resolving_an_unknown_approval_is_rejected(database):
    with pytest.raises(ValueError, match="does-not-exist"):
        ApprovalStore(database=database).resolve("does-not-exist", approved=True)


def test_runs_persist_and_reload(database):
    store = RunStore(database=database)

    run_id = store.create_run("Investigate batch 43.")

    reloaded = RunStore(database=database).get_run(run_id)

    assert reloaded is not None
    assert reloaded.status == RunStatus.RUNNING.value
    assert reloaded.user_message == "Investigate batch 43."
    assert reloaded.final_answer is None


def test_run_lifecycle_transitions(database):
    store = RunStore(database=database)

    run_id = store.create_run("hello")

    store.await_approval(run_id, [{"role": "user", "content": "hello"}])

    assert store.get_run(run_id).status == RunStatus.WAITING_FOR_APPROVAL.value

    store.resume(run_id)

    assert store.get_run(run_id).status == RunStatus.RUNNING.value

    store.complete(run_id, "done")

    record = store.get_run(run_id)

    assert record.status == RunStatus.COMPLETED.value
    assert record.final_answer == "done"


def test_run_steps_are_numbered_and_ordered(database):
    from app.run_store import StepType

    store = RunStore(database=database)

    run_id = store.create_run("hello")

    store.add_step(run_id, StepType.MODEL_RESPONSE, result={"text": None})
    store.add_step(run_id, StepType.TOOL_REQUESTED, tool_name="t", arguments={"a": 1})
    store.add_step(run_id, StepType.TOOL_EXECUTED, tool_name="t", result={"ok": True})

    steps = RunStore(database=database).list_steps(run_id)

    assert [step["step_number"] for step in steps] == [1, 2, 3]
    assert [step["step_type"] for step in steps] == [
        "MODEL_RESPONSE",
        "TOOL_REQUESTED",
        "TOOL_EXECUTED",
    ]
    assert steps[1]["arguments"] == {"a": 1}
    assert steps[2]["result"] == {"ok": True}


def test_updating_an_unknown_run_is_rejected(database):
    with pytest.raises(ValueError, match="Unknown run ID"):
        RunStore(database=database).complete("nope", "answer")


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
