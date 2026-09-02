from fastapi import FastAPI, HTTPException

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.database import Database
from app.model_provider import (
    OpenAIModelProvider,
)
from app.models import (
    AgentRequest,
    AgentResponse,
    ApprovalDecision,
    ApprovalResponse,
    AuditEvent,
)
from app.tool_registry import (
    Tool,
    ToolRegistry,
    ToolRisk,
)
from app.tools import (
    calculator,
    get_migration_status,
    restart_migration,
)

app = FastAPI(title="Enterprise AgentOps")

model_provider = OpenAIModelProvider()

database = Database()

tool_registry = ToolRegistry()
approval_store = ApprovalStore(database=database)
audit_store = AuditStore(database=database)


tool_registry.register(
    Tool(
        name="calculator",
        description=("Perform a basic arithmetic operation."),
        function=calculator,
        risk=ToolRisk.READ,
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
                "operation": {
                    "type": "string",
                    "enum": [
                        "add",
                        "subtract",
                        "multiply",
                        "divide",
                    ],
                },
            },
            "required": [
                "a",
                "b",
                "operation",
            ],
            "additionalProperties": False,
        },
    )
)


tool_registry.register(
    Tool(
        name="get_migration_status",
        description=(
            "Get the actual migration status and error details for a specific batch ID."
        ),
        function=get_migration_status,
        risk=ToolRisk.READ,
        parameters={
            "type": "object",
            "properties": {
                "batch_id": {"type": "integer"},
            },
            "required": ["batch_id"],
            "additionalProperties": False,
        },
    )
)


tool_registry.register(
    Tool(
        name="restart_migration",
        description=("Restart a failed migration batch."),
        function=restart_migration,
        risk=ToolRisk.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "batch_id": {"type": "integer"},
            },
            "required": ["batch_id"],
            "additionalProperties": False,
        },
    )
)


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
