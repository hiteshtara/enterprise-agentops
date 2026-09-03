"""The core RBAC demo: an operator proposes, only an approver may release."""

from app.approval_store import ApprovalStatus
from app.identity import Role
from app.run_store import RunStatus
from tests.fakes import ScriptedModelProvider, final_response, tool_response

RESTART_TOOL = "restart_migration"
GET_STATUS_TOOL = "get_migration_status"


def script() -> ScriptedModelProvider:
    return ScriptedModelProvider(
        [
            tool_response(GET_STATUS_TOOL, {"batch_id": 43}, "c-read"),
            tool_response(RESTART_TOOL, {"batch_id": 43}, "c-write"),
            final_response("Batch 43 failed on an Oracle timeout and was restarted."),
        ]
    )


def start_waiting_run(api, role: str = "OPERATOR"):
    """Run the agent until it parks on a WRITE approval."""
    module = api.module

    from app.seed_data import seed_migration_batches

    seed_migration_batches(module.database)

    module.agent.model = script()

    response = api.client(role).post(
        "/agent/run",
        json={"message": "Investigate migration batch 43 and restart it if needed."},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == RunStatus.WAITING_FOR_APPROVAL.value

    return body


def user_id(api, role: str) -> str:
    email, _ = api.credentials[role]

    return api.module.user_store.get_by_email(email).user_id


# -- attribution -----------------------------------------------------------


def test_run_is_attributed_to_the_authenticated_caller(api):
    body = start_waiting_run(api)

    run = api.module.run_store.get_run(body["run_id"])

    assert run.requested_by_user_id == user_id(api, "OPERATOR")


def test_identity_cannot_be_spoofed_through_the_request_body(api):
    module = api.module

    from app.seed_data import seed_migration_batches

    seed_migration_batches(module.database)

    module.agent.model = ScriptedModelProvider([final_response("Hi.")])

    response = api.client("OPERATOR").post(
        "/agent/run",
        json={
            "message": "Hello.",
            "requested_by_user_id": "somebody-else",
            "user_id": "somebody-else",
            "role": "ADMIN",
        },
    )

    assert response.status_code == 200

    run = module.run_store.get_run(response.json()["run_id"])

    assert run.requested_by_user_id == user_id(api, "OPERATOR")


def test_approval_records_the_requesting_user(api):
    body = start_waiting_run(api)

    approval = api.module.approval_store.get(
        body["approval_required"]["approval_id"],
    )

    assert approval.requested_by_user_id == user_id(api, "OPERATOR")
    assert approval.resolved_by_user_id is None


def test_audit_events_carry_the_actor(api):
    body = start_waiting_run(api)

    events = api.module.audit_store.list_events(run_id=body["run_id"])

    assert events
    assert all(event["actor_user_id"] == user_id(api, "OPERATOR") for event in events)


# -- authorization ---------------------------------------------------------


def test_operator_cannot_approve_a_write(api):
    body = start_waiting_run(api)

    approval_id = body["approval_required"]["approval_id"]

    response = api.client("OPERATOR").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    assert response.status_code == 403
    assert "APPROVE_WRITE" in response.json()["detail"]


def test_a_denied_approval_does_not_execute_the_tool(api):
    body = start_waiting_run(api)

    approval_id = body["approval_required"]["approval_id"]

    api.client("OPERATOR").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    executed = [
        event
        for event in api.module.audit_store.list_events(run_id=body["run_id"])
        if event["event_type"] == "TOOL_EXECUTED"
    ]

    assert all(event["details"]["tool"] != RESTART_TOOL for event in executed)


def test_a_denied_approval_stays_pending(api):
    body = start_waiting_run(api)

    approval_id = body["approval_required"]["approval_id"]

    api.client("OPERATOR").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    approval = api.module.approval_store.get(approval_id)

    assert approval.status == ApprovalStatus.PENDING.value
    assert approval.resolved_by_user_id is None
    assert approval.resolved_at is None

    run = api.module.run_store.get_run(body["run_id"])

    assert run.status == RunStatus.WAITING_FOR_APPROVAL.value


def test_a_denied_approval_is_audited(api):
    body = start_waiting_run(api)

    approval_id = body["approval_required"]["approval_id"]

    api.client("OPERATOR").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    denied = [
        event
        for event in api.module.audit_store.list_events(run_id=body["run_id"])
        if event["event_type"] == "AUTHORIZATION_DENIED"
    ]

    assert len(denied) == 1
    assert denied[0]["actor_user_id"] == user_id(api, "OPERATOR")
    assert denied[0]["details"]["required_permission"] == "APPROVE_WRITE"
    assert denied[0]["details"]["role"] == Role.OPERATOR.value


def test_operator_cannot_reject_either(api):
    """Blocking an action is the same authority as releasing it."""
    body = start_waiting_run(api)

    response = api.client("OPERATOR").post(
        f"/agent/approvals/{body['approval_required']['approval_id']}",
        json={"approved": False},
    )

    assert response.status_code == 403

    approval = api.module.approval_store.get(
        body["approval_required"]["approval_id"],
    )

    assert approval.status == ApprovalStatus.PENDING.value


def test_viewer_cannot_approve(api):
    body = start_waiting_run(api)

    response = api.client("VIEWER").post(
        f"/agent/approvals/{body['approval_required']['approval_id']}",
        json={"approved": True},
    )

    assert response.status_code == 403


def test_anonymous_cannot_approve(api):
    body = start_waiting_run(api)

    response = api.anonymous().post(
        f"/agent/approvals/{body['approval_required']['approval_id']}",
        json={"approved": True},
    )

    assert response.status_code == 401


# -- the approver path -----------------------------------------------------


def test_approver_resumes_the_operators_run(api):
    body = start_waiting_run(api)

    approval_id = body["approval_required"]["approval_id"]

    response = api.client("APPROVER").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    assert response.status_code == 200

    resumed = response.json()

    assert resumed["run_id"] == body["run_id"]
    assert resumed["run_status"] == RunStatus.COMPLETED.value
    assert "restarted" in resumed["answer"].lower()


def test_resolution_records_who_decided(api):
    body = start_waiting_run(api)

    approval_id = body["approval_required"]["approval_id"]

    api.client("APPROVER").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    approval = api.module.approval_store.get(approval_id)

    assert approval.status == ApprovalStatus.APPROVED.value
    assert approval.requested_by_user_id == user_id(api, "OPERATOR")
    assert approval.resolved_by_user_id == user_id(api, "APPROVER")


def test_the_audit_trail_names_both_actors(api):
    body = start_waiting_run(api)

    api.client("APPROVER").post(
        f"/agent/approvals/{body['approval_required']['approval_id']}",
        json={"approved": True},
    )

    events = api.module.audit_store.list_events(run_id=body["run_id"])

    def one(event_type: str, tool: str | None = None):
        matches = [
            event
            for event in events
            if event["event_type"] == event_type
            and (tool is None or event["details"].get("tool") == tool)
        ]

        assert len(matches) == 1, f"{event_type}/{tool}: {len(matches)} matches"

        return matches[0]

    operator = user_id(api, "OPERATOR")
    approver = user_id(api, "APPROVER")

    # The read the operator caused stays theirs; the write belongs to whoever
    # released it.
    assert one("TOOL_EXECUTED", GET_STATUS_TOOL)["actor_user_id"] == operator
    assert one("APPROVAL_REQUIRED")["actor_user_id"] == operator
    assert one("APPROVAL_GRANTED")["actor_user_id"] == approver
    assert one("TOOL_EXECUTED", RESTART_TOOL)["actor_user_id"] == approver


def test_admin_may_also_approve_a_write(api):
    body = start_waiting_run(api)

    response = api.client("ADMIN").post(
        f"/agent/approvals/{body['approval_required']['approval_id']}",
        json={"approved": True},
    )

    assert response.status_code == 200


def test_rejecting_cancels_the_run(api):
    body = start_waiting_run(api)

    response = api.client("APPROVER").post(
        f"/agent/approvals/{body['approval_required']['approval_id']}",
        json={"approved": False},
    )

    assert response.status_code == 200
    assert response.json()["run_status"] == RunStatus.CANCELLED.value


def test_an_unknown_approval_is_a_404_not_a_403(api):
    response = api.client("VIEWER").post(
        "/agent/approvals/does-not-exist",
        json={"approved": True},
    )

    assert response.status_code == 404
