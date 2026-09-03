"""Authentication endpoints and route-level authorization."""

import pytest

from app.identity import Permission, Role
from app.seed_users import DEMO_USERS, seed_demo_users
from app.user_store import UserStore
from tests.fakes import ScriptedModelProvider, final_response, tool_response

RESTART_TOOL = "restart_migration"
QUERY_TOOL = "query_migration_batches"


# -- seeding ---------------------------------------------------------------


def test_demo_users_are_seeded_idempotently(database):
    assert seed_demo_users(database) == len(DEMO_USERS)
    assert seed_demo_users(database) == 0

    store = UserStore(database=database)

    assert len(store.list_users()) == len(DEMO_USERS)
    assert {user.role for user in store.list_users()} == set(Role)


def test_seeded_passwords_are_stored_only_as_hashes(database):
    seed_demo_users(database)

    from app.db_models import UserRecord

    with database.session() as session:
        records = session.query(UserRecord).all()

        for record in records:
            assert record.password_hash.startswith("$2")

            for _, _, password, _ in DEMO_USERS:
                assert password not in record.password_hash


def test_authenticate_accepts_only_the_right_password(database):
    seed_demo_users(database)

    store = UserStore(database=database)

    assert store.authenticate("admin@agentguard.local", "admin-demo-password")
    assert store.authenticate("admin@agentguard.local", "wrong") is None
    assert store.authenticate("nobody@agentguard.local", "admin-demo-password") is None


def test_authenticate_rejects_a_deactivated_account(database):
    store = UserStore(database=database)

    store.create(
        email="gone@agentguard.local",
        display_name="Gone",
        password="still-a-valid-password",
        role=Role.ADMIN,
        active=False,
    )

    assert store.authenticate("gone@agentguard.local", "still-a-valid-password") is None


# -- login -----------------------------------------------------------------


def test_login_returns_a_token_and_the_user(api):
    response = api.anonymous().post(
        "/auth/login",
        json={
            "email": "operator@agentguard.local",
            "password": "operator-demo-password",
        },
    )

    assert response.status_code == 200

    body = response.json()

    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["role"] == "OPERATOR"
    assert body["user"]["email"] == "operator@agentguard.local"
    assert Permission.RUN_AGENT.value in body["user"]["permissions"]


def test_login_never_returns_a_password_hash(api):
    response = api.anonymous().post(
        "/auth/login",
        json={"email": "admin@agentguard.local", "password": "admin-demo-password"},
    )

    assert "password_hash" not in response.text
    assert "password" not in response.json()["user"]


def test_login_with_a_wrong_password_is_rejected(api):
    response = api.anonymous().post(
        "/auth/login",
        json={"email": "admin@agentguard.local", "password": "nope"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password."


def test_login_does_not_reveal_whether_an_account_exists(api):
    unknown = api.anonymous().post(
        "/auth/login",
        json={"email": "nobody@agentguard.local", "password": "whatever"},
    )
    wrong = api.anonymous().post(
        "/auth/login",
        json={"email": "admin@agentguard.local", "password": "whatever"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_login_rejects_a_deactivated_account(api):
    module = api.module

    module.user_store.create(
        email="gone@agentguard.local",
        display_name="Gone",
        password="still-a-valid-password",
        role=Role.ADMIN,
        active=False,
    )

    response = api.anonymous().post(
        "/auth/login",
        json={"email": "gone@agentguard.local", "password": "still-a-valid-password"},
    )

    assert response.status_code == 401


# -- current user ----------------------------------------------------------


def test_auth_me_returns_the_caller(api):
    response = api.client("APPROVER").get("/auth/me")

    assert response.status_code == 200

    body = response.json()

    assert body["role"] == "APPROVER"
    assert Permission.APPROVE_WRITE.value in body["permissions"]
    assert Permission.APPROVE_DANGEROUS.value not in body["permissions"]


def test_auth_me_requires_a_token(api):
    assert api.anonymous().get("/auth/me").status_code == 401


def test_a_malformed_token_is_rejected(api):
    http = api.anonymous()

    http.headers["Authorization"] = "Bearer not-a-real-token"

    response = http.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated."


def test_a_missing_bearer_scheme_is_rejected(api):
    http = api.anonymous()

    http.headers["Authorization"] = "some-token-without-a-scheme"

    assert http.get("/auth/me").status_code == 401


def test_a_token_for_a_deactivated_user_stops_working(api):
    http = api.client("ADMIN")

    assert http.get("/auth/me").status_code == 200

    from app.db_models import UserRecord

    with api.module.database.session() as session:
        record = session.scalar(
            session.query(UserRecord)
            .filter(UserRecord.email == "admin@agentguard.local")
            .statement
        )
        record.active = False
        session.commit()

    response = http.get("/auth/me")

    assert response.status_code == 403
    assert "deactivated" in response.json()["detail"]


# -- protected routes ------------------------------------------------------


PROTECTED = [
    ("GET", "/runs"),
    ("GET", "/runs/some-id"),
    ("GET", "/approvals"),
    ("GET", "/audit/events"),
    ("GET", "/tools"),
    ("GET", "/overview"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_routes_reject_anonymous_callers(api, method, path):
    response = api.anonymous().request(method, path)

    assert response.status_code == 401


def test_public_routes_need_no_credentials(api):
    assert api.anonymous().get("/health").status_code == 200


def test_agent_run_rejects_anonymous_callers(api):
    response = api.anonymous().post("/agent/run", json={"message": "hi"})

    assert response.status_code == 401


def test_viewer_cannot_run_the_agent(api):
    response = api.client("VIEWER").post("/agent/run", json={"message": "hi"})

    assert response.status_code == 403
    assert "RUN_AGENT" in response.json()["detail"]


def test_viewer_can_read_everything(api):
    http = api.client("VIEWER")

    for path in ("/runs", "/approvals", "/audit/events", "/tools", "/overview"):
        assert http.get(path).status_code == 200, path


def test_only_admin_may_reconcile(api):
    for role in ("VIEWER", "OPERATOR", "APPROVER"):
        assert api.client(role).post("/runs/reconcile").status_code == 403

    assert api.client("ADMIN").post("/runs/reconcile").status_code == 200


def test_operator_can_run_the_agent(api):
    module = api.module

    from app.seed_data import seed_migration_batches

    seed_migration_batches(module.database)

    module.agent.model = ScriptedModelProvider(
        [tool_response(QUERY_TOOL, {"limit": 1}), final_response("Done.")]
    )

    response = api.client("OPERATOR").post(
        "/agent/run",
        json={"message": "Show me a batch."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
