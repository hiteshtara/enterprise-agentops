"""Durable approval history: the store contract and the read-only endpoint."""

from datetime import UTC, datetime

import pytest

from app.approval_store import (
    MAX_LIMIT,
    MIN_LIMIT,
    ApprovalStatus,
    ApprovalStore,
)


def make_approval(store: ApprovalStore, run_id: str, tool: str = "restart_migration"):
    return store.create(
        tool=tool,
        arguments={"batch_id": 43},
        risk="WRITE",
        run_id=run_id,
        tool_call_id=f"call-{run_id}",
    )


@pytest.fixture
def approvals(database) -> ApprovalStore:
    """Three approvals: one PENDING, one APPROVED, one REJECTED."""
    store = ApprovalStore(database=database)

    make_approval(store, "run-pending")

    approved = make_approval(store, "run-approved")
    store.resolve(approved.approval_id, approved=True)

    rejected = make_approval(store, "run-rejected")
    store.resolve(rejected.approval_id, approved=False)

    return store


def test_list_returns_every_approval(approvals):
    listed = approvals.list_approvals()

    assert len(listed) == 3

    assert {item["status"] for item in listed} == {
        "PENDING",
        "APPROVED",
        "REJECTED",
    }


def test_list_returns_the_documented_fields(approvals):
    item = approvals.list_approvals(status="PENDING")[0]

    assert set(item) == {
        "approval_id",
        "run_id",
        "requested_by_user_id",
        "resolved_by_user_id",
        "tool",
        "arguments",
        "risk",
        "status",
        "created_at",
        "resolved_at",
        "decision",
    }
    assert item["run_id"] == "run-pending"
    assert item["arguments"] == {"batch_id": 43}
    assert item["risk"] == "WRITE"


def test_list_filters_by_status(approvals):
    for status in ApprovalStatus:
        listed = approvals.list_approvals(status=status.value)

        assert len(listed) == 1
        assert listed[0]["status"] == status.value


def test_list_filters_by_run(approvals):
    listed = approvals.list_approvals(run_id="run-approved")

    assert len(listed) == 1
    assert listed[0]["run_id"] == "run-approved"


def test_list_applies_the_limit(approvals):
    assert len(approvals.list_approvals(limit=2)) == 2
    assert len(approvals.list_approvals(limit=MIN_LIMIT)) == 1


def test_limit_out_of_range_is_rejected(approvals):
    with pytest.raises(ValueError, match="between 1 and 100"):
        approvals.list_approvals(limit=MAX_LIMIT + 1)

    with pytest.raises(ValueError, match="between 1 and 100"):
        approvals.list_approvals(limit=MIN_LIMIT - 1)


def test_non_integer_limit_is_rejected(approvals):
    with pytest.raises(TypeError, match="must be an integer"):
        approvals.list_approvals(limit="20")


def test_invalid_status_is_rejected(approvals):
    with pytest.raises(ValueError, match="Unsupported approval status"):
        approvals.list_approvals(status="EXPIRED")


def test_lowercase_status_is_rejected(approvals):
    with pytest.raises(ValueError, match="Unsupported approval status"):
        approvals.list_approvals(status="pending")


def test_history_survives_resolution(database):
    store = ApprovalStore(database=database)

    created = make_approval(store, "run-1")

    store.resolve(created.approval_id, approved=True)

    listed = store.list_approvals(status="APPROVED")

    assert len(listed) == 1
    assert listed[0]["approval_id"] == created.approval_id
    assert listed[0]["decision"] == "APPROVED"
    assert listed[0]["resolved_at"] is not None

    # It left PENDING rather than being deleted.
    assert store.list_approvals(status="PENDING") == []


def test_listing_does_not_touch_the_development_database(
    approvals,
    development_database_path,
):
    before = (
        development_database_path.stat() if development_database_path.exists() else None
    )

    approvals.list_approvals()

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


@pytest.fixture
def client(api):
    store = api.module.approval_store

    make_approval(store, "run-pending")

    approved = make_approval(store, "run-approved")
    store.resolve(approved.approval_id, approved=True)

    return api.client("ADMIN")


def test_endpoint_lists_approvals(client):
    response = client.get("/approvals")

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_endpoint_filters_by_status(client):
    response = client.get("/approvals", params={"status": "APPROVED"})

    assert response.status_code == 200

    body = response.json()

    assert len(body) == 1
    assert body[0]["status"] == "APPROVED"
    assert body[0]["run_id"] == "run-approved"


def test_endpoint_rejects_an_invalid_status(client):
    response = client.get("/approvals", params={"status": "EXPIRED"})

    assert response.status_code == 400
    assert "Unsupported approval status" in response.json()["detail"]


def test_endpoint_bounds_the_limit(client):
    assert client.get("/approvals", params={"limit": 1}).status_code == 200
    assert len(client.get("/approvals", params={"limit": 1}).json()) == 1

    assert client.get("/approvals", params={"limit": 0}).status_code == 422
    assert client.get("/approvals", params={"limit": 101}).status_code == 422


def test_endpoint_exposes_no_callable_internals(client):
    item = client.get("/approvals").json()[0]

    assert "function" not in item
    assert "tool_call_id" not in item

    for value in item.values():
        assert isinstance(value, (str, dict, type(None)))


def test_endpoint_defaults_to_a_bounded_limit(client):
    store = None

    import app.main

    store = app.main.approval_store

    for index in range(30):
        make_approval(store, f"bulk-{index}")

    assert len(client.get("/approvals").json()) == 20


def test_created_at_is_iso_utc(approvals):
    created_at = approvals.list_approvals()[0]["created_at"]

    parsed = datetime.fromisoformat(created_at)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(None)
