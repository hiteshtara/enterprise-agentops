from fastapi import FastAPI, HTTPException, Query

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.database import Database
from app.migration_store import MigrationBatchStore
from app.model_provider import OpenAIModelProvider
from app.models import (
    AgentRequest,
    AgentResponse,
    ApprovalDecision,
    ApprovalResponse,
    AuditEvent,
    RunDetail,
    RunSummary,
)
from app.run_store import RunStore
from app.tool_setup import build_tool_registry

app = FastAPI(title="AgentGuard")

database = Database()

model_provider = OpenAIModelProvider()

approval_store = ApprovalStore(database=database)
audit_store = AuditStore(database=database)
run_store = RunStore(database=database)
migration_store = MigrationBatchStore(database=database)

tool_registry = build_tool_registry(migration_store=migration_store)

agent = AgentService(
    model=model_provider,
    tool_registry=tool_registry,
    approval_store=approval_store,
    audit_store=audit_store,
    run_store=run_store,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/agent/run",
    response_model=AgentResponse,
)
def run_agent(
    request: AgentRequest,
) -> AgentResponse:
    result = agent.run(request.message)

    return AgentResponse(**result)


@app.post(
    "/agent/approvals/{approval_id}",
    response_model=ApprovalResponse,
)
def resolve_approval(
    approval_id: str,
    decision: ApprovalDecision,
) -> ApprovalResponse:
    try:
        result = agent.resolve_approval(
            approval_id=approval_id,
            approved=decision.approved,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    return ApprovalResponse(**result)


@app.get(
    "/runs",
    response_model=list[RunSummary],
)
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RunSummary]:
    return [RunSummary(**run) for run in run_store.list_runs(limit=limit)]


@app.get(
    "/runs/{run_id}",
    response_model=RunDetail,
)
def get_run(
    run_id: str,
) -> RunDetail:
    record = run_store.get_run(run_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown run ID: {run_id}",
        )

    return RunDetail(
        run_id=record.run_id,
        status=record.status,
        user_message=record.user_message,
        final_answer=record.final_answer,
        created_at=record.created_at,
        updated_at=record.updated_at,
        steps=run_store.list_steps(run_id),
    )


@app.get(
    "/audit/events",
    response_model=list[AuditEvent],
)
def get_audit_events(
    run_id: str | None = Query(default=None),
) -> list[AuditEvent]:
    events = audit_store.list_events(run_id=run_id)

    return [AuditEvent(**event) for event in events]
