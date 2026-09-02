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
    ApprovalSummary,
    AuditEvent,
    ReconcileResponse,
    RunDetail,
    RunSummary,
)
from app.reconciliation import (
    DEFAULT_STALE_AFTER_SECONDS,
    MAX_STALE_AFTER_SECONDS,
    MIN_STALE_AFTER_SECONDS,
    ReconciliationService,
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

reconciliation = ReconciliationService(
    run_store=run_store,
    audit_store=audit_store,
)

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
    "/approvals",
    response_model=list[ApprovalSummary],
)
def list_approvals(
    status: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ApprovalSummary]:
    try:
        approvals = approval_store.list_approvals(
            status=status,
            run_id=run_id,
            limit=limit,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return [ApprovalSummary(**approval) for approval in approvals]


@app.post(
    "/runs/reconcile",
    response_model=ReconcileResponse,
)
def reconcile_runs(
    stale_after_seconds: int = Query(
        default=DEFAULT_STALE_AFTER_SECONDS,
        ge=MIN_STALE_AFTER_SECONDS,
        le=MAX_STALE_AFTER_SECONDS,
    ),
) -> ReconcileResponse:
    """Mark RUNNING runs abandoned by a crashed process as FAILED."""
    service = ReconciliationService(
        run_store=run_store,
        audit_store=audit_store,
        stale_after_seconds=stale_after_seconds,
    )

    reconciled = service.reconcile()

    return ReconcileResponse(reconciled=reconciled, count=len(reconciled))


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
