from pathlib import Path

import pytest

from app.database import DEFAULT_DATABASE_URL, Database
from app.migration_store import MigrationBatchStore
from app.run_store import RunStore
from app.seed_data import seed_migration_batches
from app.tool_registry import ToolRegistry
from app.tool_setup import build_tool_registry


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """An isolated, schema-initialised SQLite database for a single test.

    pytest's tmp_path is unique per test, so every test starts empty and
    nothing is written to the development database.
    """
    database = Database(url=f"sqlite:///{tmp_path / 'isolated.db'}")

    database.create_all()

    yield database

    database.dispose()


@pytest.fixture
def development_database_path() -> Path:
    """Path to the local development database referenced by the default URL."""
    return Path(DEFAULT_DATABASE_URL.removeprefix("sqlite:///"))


@pytest.fixture
def seeded_database(database: Database) -> Database:
    """An isolated database with the development migration batches loaded."""
    seed_migration_batches(database)

    return database


@pytest.fixture
def migration_store(seeded_database: Database) -> MigrationBatchStore:
    return MigrationBatchStore(database=seeded_database)


@pytest.fixture
def registry(migration_store: MigrationBatchStore) -> ToolRegistry:
    """The real application tool registry, backed by the isolated database."""
    return build_tool_registry(migration_store=migration_store)


@pytest.fixture
def run_store(database: Database) -> RunStore:
    return RunStore(database=database)


@pytest.fixture
def agent_factory(database: Database, registry: ToolRegistry):
    """Build an AgentService against the isolated database.

    Takes a model provider so each test scripts its own model behaviour, and
    optionally a different registry or iteration budget.
    """
    from app.agent import DEFAULT_MAX_ITERATIONS, AgentService
    from app.approval_store import ApprovalStore
    from app.audit_store import AuditStore
    from app.observability_store import ModelExecutionStore, ToolExecutionStore

    def build(
        model,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        monotonic_ns=None,
    ) -> AgentService:
        return AgentService(
            model=model,
            tool_registry=tool_registry or registry,
            approval_store=ApprovalStore(database=database),
            audit_store=AuditStore(database=database),
            run_store=RunStore(database=database),
            max_iterations=max_iterations,
            model_executions=ModelExecutionStore(database=database),
            tool_executions=ToolExecutionStore(database=database),
            monotonic_ns=monotonic_ns,
        )

    return build


@pytest.fixture
def api(monkeypatch, tmp_path):
    """A reloaded app bound to an isolated database, plus logged-in clients.

    Returns an object exposing:
      api.module            the reloaded app.main
      api.client(role)      a TestClient carrying that demo role's bearer token
      api.anonymous()       a TestClient with no credentials
    """
    import importlib
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.seed_users import DEMO_USERS, seed_demo_users

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(
        "AGENTOPS_DATABASE_URL",
        f"sqlite:///{tmp_path / 'api.db'}",
    )
    # At least 32 bytes, or PyJWT warns about HMAC key length.
    monkeypatch.setenv(
        "AGENTGUARD_AUTH_SECRET",
        "test-only-signing-secret-not-for-any-real-deployment",
    )
    # Keep bcrypt cheap in tests; production uses the default cost factor.
    monkeypatch.setenv("AGENTGUARD_BCRYPT_ROUNDS", "4")

    import app.main

    module = importlib.reload(app.main)

    module.database.create_all()
    seed_demo_users(module.database)

    credentials = {
        role.value: (email, password) for email, _, password, role in DEMO_USERS
    }

    def client(role: str = "ADMIN") -> TestClient:
        email, password = credentials[role]

        http = TestClient(module.app)

        response = http.post(
            "/auth/login",
            json={"email": email, "password": password},
        )

        assert response.status_code == 200, response.text

        http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

        return http

    return SimpleNamespace(
        module=module,
        client=client,
        anonymous=lambda: TestClient(module.app),
        credentials=credentials,
    )
