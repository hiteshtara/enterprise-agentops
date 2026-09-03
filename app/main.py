from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.auth import (
    current_user,
    require_reconcile_runs,
    require_run_agent,
    require_view_approvals,
    require_view_audit,
    require_view_runs,
    require_view_tools,
)
from app.authorization import ensure_can_resolve_approval
from app.connectors.lodgify.client import LodgifyClient
from app.connectors.lodgify.config import is_configured, resolve_api_key
from app.connectors.lodgify.errors import (
    LodgifyConfigurationError,
    LodgifyUnavailable,
)
from app.connectors.lodgify.inbox import LodgifyInbox
from app.connectors.lodgify.messaging_client import LodgifyMessagingClient
from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools
from app.connectors.lodgify.tools import LodgifyTools
from app.database import Database
from app.identity import PermissionDenied, User
from app.migration_store import MigrationBatchStore
from app.model_provider import OpenAIModelProvider
from app.models import (
    AgentRequest,
    AgentResponse,
    ApprovalDecision,
    ApprovalResponse,
    ApprovalSummary,
    AuditEvent,
    ConversationDetail,
    CurrentUser,
    GuestReplyRequest,
    InboxPage,
    LoginRequest,
    LoginResponse,
    Overview,
    ReconcileResponse,
    RunDetail,
    RunMetrics,
    RunSummary,
    ToolSummary,
)
from app.observability_store import (
    ModelExecutionStore,
    RunMetricsService,
    ToolExecutionStore,
)
from app.overview import OverviewService
from app.reconciliation import (
    DEFAULT_STALE_AFTER_SECONDS,
    MAX_STALE_AFTER_SECONDS,
    MIN_STALE_AFTER_SECONDS,
    ReconciliationService,
)
from app.run_store import RunStore
from app.security import issue_token
from app.tool_setup import build_tool_registry
from app.user_store import UserStore, user_to_dict

app = FastAPI(title="AgentGuard")

# Local development only: the Vite dev server runs on a different port, so the
# console origin is named explicitly. Never a wildcard. In production the
# console and API are served from one origin and this list is not used.
LOCAL_CONSOLE_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_CONSOLE_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    # Authorization is required: the console sends a bearer token, which makes
    # every request preflighted. Omitting it fails the preflight in a browser
    # while leaving TestClient (which does not preflight) passing.
    allow_headers=["Authorization", "Content-Type"],
)

database = Database()

model_provider = OpenAIModelProvider()

approval_store = ApprovalStore(database=database)
audit_store = AuditStore(database=database)
run_store = RunStore(database=database)
model_execution_store = ModelExecutionStore(database=database)
tool_execution_store = ToolExecutionStore(database=database)
run_metrics = RunMetricsService(database=database)
migration_store = MigrationBatchStore(database=database)
user_store = UserStore(database=database)

# The connector is wired only when a credential is configured. Importing the
# app never reads the key, and its absence is not a startup failure.
lodgify_configured = is_configured()

lodgify_tools = (
    LodgifyTools(LodgifyClient(api_key_provider=resolve_api_key))
    if lodgify_configured
    else None
)

# The inbox shares the credential resolver but not the read-only client: the
# messaging transport is the one place that issues a Lodgify write, and it is
# kept separate so `LodgifyClient` stays a read-only object.
lodgify_inbox = (
    LodgifyInbox(LodgifyMessagingClient(api_key_provider=resolve_api_key))
    if lodgify_configured
    else None
)

tool_registry = build_tool_registry(
    migration_store=migration_store,
    lodgify=lodgify_tools,
    lodgify_messaging=(
        LodgifyMessagingTools(lodgify_inbox) if lodgify_inbox is not None else None
    ),
)

# Auth dependencies resolve the store from app state so tests can swap the
# database without patching module globals.
app.state.user_store = user_store

reconciliation = ReconciliationService(
    run_store=run_store,
    audit_store=audit_store,
)

overview_service = OverviewService(
    run_store=run_store,
    approval_store=approval_store,
    audit_store=audit_store,
)

agent = AgentService(
    model=model_provider,
    tool_registry=tool_registry,
    approval_store=approval_store,
    audit_store=audit_store,
    run_store=run_store,
    model_executions=model_execution_store,
    tool_executions=tool_execution_store,
)


@app.post(
    "/auth/login",
    response_model=LoginResponse,
)
def login(request: LoginRequest) -> LoginResponse:
    user = user_store.authenticate(request.email, request.password)

    if user is None:
        # One message for unknown email, wrong password and deactivated
        # account, so the response cannot be used to enumerate identities.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    return LoginResponse(
        access_token=issue_token(user.user_id),
        user=CurrentUser(**user_to_dict(user)),
    )


@app.get(
    "/auth/me",
    response_model=CurrentUser,
)
def read_current_user(user: User = Depends(current_user)) -> CurrentUser:
    return CurrentUser(**user_to_dict(user))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/agent/run",
    response_model=AgentResponse,
)
def run_agent(
    request: AgentRequest,
    user: User = Depends(require_run_agent),
) -> AgentResponse:
    # Identity comes from the token, never from the request body.
    result = agent.run(request.message, actor_user_id=user.user_id)

    return AgentResponse(**result)


@app.post(
    "/agent/approvals/{approval_id}",
    response_model=ApprovalResponse,
)
def resolve_approval(
    approval_id: str,
    decision: ApprovalDecision,
    user: User = Depends(current_user),
) -> ApprovalResponse:
    pending = approval_store.get(approval_id)

    if pending is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown approval ID: {approval_id}",
        )

    try:
        # Authorization is decided from the approval's own risk tier before
        # anything is resolved or executed.
        ensure_can_resolve_approval(user, pending.risk)

    except PermissionDenied as exc:
        audit_store.record(
            "AUTHORIZATION_DENIED",
            {
                "approval_id": approval_id,
                "tool": pending.tool,
                "risk": pending.risk,
                "required_permission": exc.permission.value,
                "role": user.role.value,
            },
            run_id=pending.run_id,
            actor_user_id=user.user_id,
        )

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    try:
        result = agent.resolve_approval(
            approval_id=approval_id,
            approved=decision.approved,
            actor_user_id=user.user_id,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    return ApprovalResponse(**result)


SEND_GUEST_REPLY_TOOL = "send_guest_reply"

INBOX_UNAVAILABLE = "The Lodgify connector is not configured."


def require_inbox() -> LodgifyInbox:
    if lodgify_inbox is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=INBOX_UNAVAILABLE,
        )

    return lodgify_inbox


@app.get(
    "/inbox",
    response_model=InboxPage,
)
def get_inbox(
    user: User = Depends(require_view_runs),
    property_slug: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> InboxPage:
    """Recent guest conversations, read live from Lodgify.

    The console polls this while the Inbox is open, which is the whole of V1's
    "notice a new message" mechanism -- no webhook, no background worker, no
    process that keeps calling Lodgify when nobody is looking.
    """
    inbox = require_inbox()

    try:
        conversations = inbox.list_conversations(
            property_slug=property_slug,
            limit=limit,
        )

    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    except LodgifyConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=INBOX_UNAVAILABLE,
        ) from exc

    except LodgifyUnavailable as exc:
        # The provider's own words are never forwarded, only our translation.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Conversations could not be loaded from the provider.",
        ) from exc

    return InboxPage(conversations=conversations, count=len(conversations))


@app.get(
    "/inbox/{conversation_ref}",
    response_model=ConversationDetail,
)
def get_conversation(
    conversation_ref: str,
    user: User = Depends(require_view_runs),
) -> ConversationDetail:
    inbox = require_inbox()

    try:
        conversation = inbox.get_conversation(conversation_ref)

    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    except LodgifyConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=INBOX_UNAVAILABLE,
        ) from exc

    except LodgifyUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The conversation could not be loaded from the provider.",
        ) from exc

    return ConversationDetail(**conversation)


@app.post(
    "/inbox/{conversation_ref}/reply",
    response_model=AgentResponse,
)
def request_guest_reply(
    conversation_ref: str,
    reply: GuestReplyRequest,
    user: User = Depends(require_run_agent),
) -> AgentResponse:
    """Submit a composed reply for approval. **This does not send anything.**

    It creates a normal governed run whose single pending action is
    `send_guest_reply` with exactly the text supplied. The tool is DANGEROUS, so
    `ToolRegistry.execute` parks it for a human just as it would for a
    model-initiated call; the console has no path that reaches Lodgify directly.

    The text is passed through untouched so the string an approver reads is the
    string the guest receives.
    """
    require_inbox()

    if tool_registry.get(SEND_GUEST_REPLY_TOOL) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=INBOX_UNAVAILABLE,
        )

    result = agent.request_action(
        tool=SEND_GUEST_REPLY_TOOL,
        arguments={
            "conversation_ref": conversation_ref,
            "subject": reply.subject,
            "message": reply.message,
        },
        summary=f"Send a guest reply on conversation {conversation_ref}.",
        actor_user_id=user.user_id,
    )

    return AgentResponse(**result)


@app.get(
    "/overview",
    response_model=Overview,
)
def get_overview(
    user: User = Depends(require_view_runs),
) -> Overview:
    return Overview(**overview_service.build())


@app.get(
    "/tools",
    response_model=list[ToolSummary],
)
def list_tools(
    user: User = Depends(require_view_tools),
) -> list[ToolSummary]:
    """Registered tools and their governance metadata. No callables."""
    return [ToolSummary(**tool) for tool in tool_registry.describe()]


@app.get(
    "/approvals",
    response_model=list[ApprovalSummary],
)
def list_approvals(
    user: User = Depends(require_view_approvals),
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
    user: User = Depends(require_reconcile_runs),
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
    user: User = Depends(require_view_runs),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RunSummary]:
    try:
        runs = run_store.list_runs(status=status, limit=limit)

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return [RunSummary(**run) for run in runs]


@app.get(
    "/runs/{run_id}",
    response_model=RunDetail,
)
def get_run(
    run_id: str,
    user: User = Depends(require_view_runs),
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
        requested_by_user_id=record.requested_by_user_id,
        user_message=record.user_message,
        final_answer=record.final_answer,
        created_at=record.created_at,
        updated_at=record.updated_at,
        steps=run_store.list_steps(run_id),
    )


@app.get(
    "/runs/{run_id}/metrics",
    response_model=RunMetrics,
)
def get_run_metrics(
    run_id: str,
    user: User = Depends(require_view_runs),
) -> RunMetrics:
    """Measured execution metrics for a run. Follows VIEW_RUNS."""
    if run_store.get_run(run_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown run ID: {run_id}",
        )

    return RunMetrics(**run_metrics.build(run_id))


@app.get(
    "/audit/events",
    response_model=list[AuditEvent],
)
def get_audit_events(
    user: User = Depends(require_view_audit),
    run_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEvent]:
    events = audit_store.list_events(
        run_id=run_id,
        event_type=event_type,
        limit=limit,
    )

    return [AuditEvent(**event) for event in events]
