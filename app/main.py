from fastapi import FastAPI, HTTPException

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
)
from app.tool_setup import build_tool_registry

app = FastAPI(title="Enterprise AgentOps")

database = Database()

model_provider = OpenAIModelProvider()

approval_store = ApprovalStore(database=database)
audit_store = AuditStore(database=database)
migration_store = MigrationBatchStore(database=database)

tool_registry = build_tool_registry(migration_store=migration_store)

agent = AgentService(
    model=model_provider,
    tool_registry=tool_registry,
    approval_store=approval_store,
    audit_store=audit_store,
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
    "/audit/events",
    response_model=list[AuditEvent],
)
def get_audit_events() -> list[AuditEvent]:
    events = audit_store.list_events()

    return [AuditEvent(**event) for event in events]
