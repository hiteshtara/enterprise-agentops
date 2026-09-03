import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openai import OpenAIError

from app.model_provider import OpenAIModelProvider

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_app_main_imports_without_openai_api_key(tmp_path):
    """A subprocess with no OPENAI_API_KEY must still be able to import the app."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OPENAI_API_KEY", "OPENAI_BASE_URL"}
    }
    env["AGENTOPS_DATABASE_URL"] = f"sqlite:///{tmp_path / 'import.db'}"

    source = (
        "import os; assert 'OPENAI_API_KEY' not in os.environ; "
        "import app.main; "
        "print(sorted(d.name for d in app.main.tool_registry.definitions()))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "query_migration_batches" in completed.stdout


def test_importing_app_main_creates_no_database_file(tmp_path):
    """Wiring the app must not connect to or create the configured database."""
    database_path = tmp_path / "untouched.db"

    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env["AGENTOPS_DATABASE_URL"] = f"sqlite:///{database_path}"

    completed = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not database_path.exists()


def test_openai_client_is_not_built_until_used(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    provider = OpenAIModelProvider()

    assert provider._client is None


def test_openai_client_still_validates_credentials_on_use(monkeypatch):
    """Laziness must not weaken credential validation at call time."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    provider = OpenAIModelProvider()

    with pytest.raises(OpenAIError, match="api_key"):
        _ = provider.client


def test_health_route_without_openai_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from app.main import app

    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_audit_route_reads_the_configured_database(api):
    module = api.module

    module.audit_store.record("TOOL_REQUESTED", {"tool": "query_migration_batches"})

    response = api.client("ADMIN").get("/audit/events")

    assert response.status_code == 200

    events = response.json()

    assert len(events) == 1
    assert events[0]["event_type"] == "TOOL_REQUESTED"
    assert events[0]["details"]["tool"] == "query_migration_batches"


def test_agent_run_route_uses_the_registry(api):
    """POST /agent/run reaches the database tool without any OpenAI call."""
    module = api.module

    from app.seed_data import seed_migration_batches
    from tests.fakes import ScriptedModelProvider, final_response, tool_response

    seed_migration_batches(module.database)

    module.agent.model = ScriptedModelProvider(
        [
            tool_response(
                "query_migration_batches",
                {"status": "FAILED", "limit": 5},
            ),
            final_response("Five batches failed."),
        ]
    )

    response = api.client("OPERATOR").post(
        "/agent/run",
        json={"message": "Show failed batches."},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["approval_required"] is None
    assert body["trace"][0]["tool"] == "query_migration_batches"
    assert all(row["status"] == "FAILED" for row in body["trace"][0]["result"])
