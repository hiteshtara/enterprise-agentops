import logging
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.auth import (
    current_user,
    require_administer,
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
from app.connectors.lodgify.messaging_models import (
    SendStatus,
    conversation_fingerprint,
)
from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools
from app.connectors.lodgify.tools import LodgifyTools
from app.conversation_activity import ConversationActivityStore
from app.conversation_refresh import ConversationRefreshService
from app.database import Database
from app.drafts import (
    SENDABLE_STATUSES,
    DraftStatus,
    DraftStore,
)
from app.historical_replies import HistoricalReplyStore, index_one_conversation
from app.identity import PermissionDenied, User
from app.inbox_view import build_inbox
from app.knowledge import KnowledgeStatus, KnowledgeStore
from app.knowledge_conflicts import find_conflicts
from app.knowledge_topics import GUEST_FACING, INTERNAL_OPERATION
from app.lodgify_webhooks import WebhookLog, handle_event, parse_webhook_body
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
    DraftEdit,
    DraftSummary,
    GuestReplyRequest,
    InboxPage,
    InboxRefreshResult,
    KnowledgeConflictSummary,
    KnowledgeCreate,
    KnowledgeEdit,
    KnowledgeItemSummary,
    KnowledgePage,
    KnowledgeSupersede,
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
from app.reply_retrieval import HistoricalReplyRetriever
from app.run_store import RunStore
from app.security import issue_token
from app.tool_setup import build_tool_registry
from app.user_store import UserStore, user_to_dict
from app.webhook_security import (
    SIGNATURE_HEADER,
    WebhookNotConfigured,
    resolve_webhook_secret,
    signature_matches,
)

logger = logging.getLogger(__name__)

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

# Drafting enrichment, not a dependency. If the index is empty -- which it is
# until someone runs `python -m app.index_lodgify_history` -- retrieval simply
# returns nothing and drafting works exactly as it did before.
historical_replies = HistoricalReplyStore(database=database)

# Owner-approved knowledge. Only APPROVED rows are ever read for drafting; a
# PROPOSED candidate is a suggestion awaiting a human and never reaches a guest.
knowledge_store = KnowledgeStore(database=database)

# Verified webhook events, in memory only. Correctness never depends on this:
# a webhook triggers a re-read of current state, which is idempotent, so a lost
# or repeated event costs nothing.
webhook_log = WebhookLog()

# Prepared replies, and the one service that prepares them. Both the webhook
# fast path and the Inbox poll converge here, so there is a single idea of what
# processing a conversation means.
draft_store = DraftStore(database=database)

activity_store = ConversationActivityStore(database=database)

tool_registry = build_tool_registry(
    migration_store=migration_store,
    lodgify=lodgify_tools,
    lodgify_messaging=(
        LodgifyMessagingTools(
            lodgify_inbox,
            retriever=HistoricalReplyRetriever(historical_replies),
            knowledge=knowledge_store,
        )
        if lodgify_inbox is not None
        else None
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

conversation_refresh = (
    ConversationRefreshService(
        inbox=lodgify_inbox,
        drafts=draft_store,
        agent=agent,
    )
    if lodgify_inbox is not None
    else None
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

    # A confirmed send is the one moment two things become true: the prepared
    # reply is spent, and the exchange is now a real example of how this owner
    # answers. Neither happens for a failed or uncertain send -- indexing an
    # unconfirmed message would teach from something that may never have
    # arrived, and marking it sent would hide that a person still needs to look.
    if decision.approved:
        settle_confirmed_send(pending.tool, result)

    return ApprovalResponse(**result)


def settle_confirmed_send(tool: str, result: dict[str, Any]) -> None:
    """After a CONFIRMED_SENT guest reply: retire the draft, learn the exchange.

    Best effort by design. Both steps are bookkeeping after an irreversible
    action that already succeeded, so a failure here must never turn a
    successful send into an error for the caller.
    """
    if tool != SEND_GUEST_REPLY_TOOL:
        return

    outcome = result.get("result")

    if not isinstance(outcome, dict):
        return

    if outcome.get("status") != SendStatus.CONFIRMED_SENT.value:
        return

    conversation_ref = outcome.get("conversation_ref")

    if not conversation_ref:
        return

    draft = draft_store.current_for(conversation_ref)

    if draft is not None and draft.status in SENDABLE_STATUSES:
        try:
            draft_store.mark_sent(draft.draft_ref)

        except ValueError:
            logger.warning("could not mark draft sent for %s", conversation_ref)

    if lodgify_inbox is None:
        return

    try:
        # The targeted learning path: one thread read, sanitized through the
        # existing pipeline, fingerprinted so a later full rebuild finds no
        # duplicate. It stops at the historical index -- nothing distils
        # knowledge or approves anything.
        created, updated = index_one_conversation(
            lodgify_inbox, historical_replies, conversation_ref
        )

        logger.info(
            "indexed sent exchange for %s: %d new, %d refreshed",
            conversation_ref,
            created,
            updated,
        )

    except Exception:  # noqa: BLE001 -- learning must never break a send
        logger.warning("post-send indexing failed for %s", conversation_ref)


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
        conversations = build_inbox(
            inbox,
            activity_store,
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

    prepared = draft_store.latest_by_conversation()

    for row in conversations:
        draft = prepared.get(row["conversation_ref"])

        # Staleness is decided here, against the fingerprint just read from the
        # provider -- so a draft written for an older state can never be shown
        # as ready.
        row["draft"] = (
            draft.to_dict(row.get("fingerprint")) if draft is not None else None
        )

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

    fingerprint = conversation_fingerprint(conversation.get("messages"))

    draft = draft_store.current_for(conversation_ref)

    return ConversationDetail(
        **conversation,
        fingerprint=fingerprint,
        draft=(
            DraftSummary(**draft.to_dict(fingerprint)) if draft is not None else None
        ),
    )


MAX_REFRESH_PER_POLL = 5


def require_refresh() -> ConversationRefreshService:
    if conversation_refresh is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=INBOX_UNAVAILABLE,
        )

    return conversation_refresh


@app.post(
    "/inbox/refresh",
    response_model=InboxRefreshResult,
)
def refresh_inbox(
    user: User = Depends(require_run_agent),
    limit: int = Query(default=20, ge=1, le=100),
) -> InboxRefreshResult:
    """Prepare replies for conversations that need one.

    The recovery path. The console calls this on a slow cadence while the Inbox
    is open, and it calls the same service the webhook does -- so a webhook that
    never arrived, or whose background task died with the process, is picked up
    here instead.

    Bounded per call: a burst of new conversations should cost a predictable
    number of model calls, not all of them at once. The rest arrive on the next
    poll.
    """
    service = require_refresh()
    inbox = require_inbox()

    try:
        conversations = inbox.list_conversations(limit=limit)

    except LodgifyUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Conversations could not be loaded from the provider.",
        ) from exc

    processed = drafted = skipped = no_reply = failed = 0

    for row in conversations:
        ref = row["conversation_ref"]

        existing = draft_store.for_state(ref, row.get("fingerprint") or "")

        if existing is not None and existing.is_settled():
            # Nothing has changed since this was worked out. No model call.
            skipped += 1
            continue

        if processed >= MAX_REFRESH_PER_POLL:
            break

        processed += 1

        result = service.process(ref, actor_user_id=user.user_id)

        if result.status == DraftStatus.DRAFT_READY.value:
            drafted += 1

        elif result.status == DraftStatus.NO_REPLY_NEEDED.value:
            no_reply += 1

        elif result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value:
            failed += 1

        elif result.skipped:
            skipped += 1

    return InboxRefreshResult(
        processed=processed,
        drafted=drafted,
        skipped=skipped,
        no_reply=no_reply,
        failed=failed,
    )


@app.get(
    "/inbox/{conversation_ref}/draft",
    response_model=DraftSummary,
)
def get_draft(
    conversation_ref: str,
    user: User = Depends(require_view_runs),
) -> DraftSummary:
    draft = draft_store.current_for(conversation_ref)

    if draft is None:
        raise HTTPException(status_code=404, detail="No prepared reply yet.")

    return DraftSummary(**draft.to_dict(current_fingerprint_for(conversation_ref)))


@app.patch(
    "/inbox/{conversation_ref}/draft",
    response_model=DraftSummary,
)
def edit_draft(
    conversation_ref: str,
    edit: DraftEdit,
    user: User = Depends(require_run_agent),
) -> DraftSummary:
    """Keep the operator's wording. Editing does not send."""
    draft = draft_store.current_for(conversation_ref)

    if draft is None:
        raise HTTPException(status_code=404, detail="No prepared reply yet.")

    try:
        updated = draft_store.edit(
            draft.draft_ref, subject=edit.subject, message=edit.message
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return DraftSummary(**updated.to_dict(current_fingerprint_for(conversation_ref)))


@app.post(
    "/inbox/{conversation_ref}/draft/regenerate",
    response_model=DraftSummary,
)
def regenerate_draft(
    conversation_ref: str,
    user: User = Depends(require_run_agent),
) -> DraftSummary:
    """Redo the work for the conversation as it stands now.

    The manual override, and the only path that sets `force` -- nothing
    automatic ever replaces a settled outcome.
    """
    service = require_refresh()

    service.process(conversation_ref, force=True, actor_user_id=user.user_id)

    draft = draft_store.current_for(conversation_ref)

    if draft is None:
        raise HTTPException(status_code=404, detail="No prepared reply yet.")

    return DraftSummary(**draft.to_dict(current_fingerprint_for(conversation_ref)))


def current_fingerprint_for(conversation_ref: str) -> str | None:
    """The live conversation's fingerprint, or None if it cannot be read.

    None means "cannot judge", and `status_for` treats that as current rather
    than stale -- a provider hiccup should not make a good draft look dangerous.
    """
    if lodgify_inbox is None:
        return None

    try:
        conversation = lodgify_inbox.get_conversation(conversation_ref)

    except (LodgifyUnavailable, ValueError):
        return None

    return conversation_fingerprint(conversation.get("messages"))


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


KNOWLEDGE_DECISIONS = {
    "approve": KnowledgeStatus.APPROVED,
    "reject": KnowledgeStatus.REJECTED,
}


@app.get(
    "/knowledge",
    response_model=KnowledgePage,
)
def list_knowledge(
    user: User = Depends(require_view_runs),
    status: str | None = Query(default=None),
    property_slug: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> KnowledgePage:
    """Every rule and candidate. Readable by anyone signed in; deciding is not."""
    try:
        items = knowledge_store.list_knowledge(
            status=status,
            property_slug=property_slug,
            limit=limit,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Conflicts are computed across every approved rule, not just the filtered
    # page: a clash the current filter hides is still a clash.
    approved = knowledge_store.list_knowledge(
        status=KnowledgeStatus.APPROVED.value,
        limit=1000,
    )

    return KnowledgePage(
        items=[KnowledgeItemSummary(**item.to_dict()) for item in items],
        counts=knowledge_store.counts(),
        conflicts=[
            KnowledgeConflictSummary(**conflict.to_dict())
            for conflict in find_conflicts(approved)
        ],
    )


KNOWLEDGE_AUDIENCES = {GUEST_FACING, INTERNAL_OPERATION}


@app.post(
    "/knowledge",
    response_model=KnowledgeItemSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_knowledge(
    payload: KnowledgeCreate,
    user: User = Depends(require_administer),
) -> KnowledgeItemSummary:
    """Author a rule directly. ADMIN only, and immediately approved."""
    if payload.audience not in KNOWLEDGE_AUDIENCES:
        raise HTTPException(
            status_code=400,
            detail=f"audience must be one of {sorted(KNOWLEDGE_AUDIENCES)}.",
        )

    try:
        item = knowledge_store.create_manual(
            property_slug=payload.property_slug,
            topic=payload.topic,
            title=payload.title,
            content=payload.content,
            audience=payload.audience,
            actor_user_id=user.user_id,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Two events, not one: authorship and approval are separate facts, and a
    # query for "everything ever approved" must find this row.
    for event in ("KNOWLEDGE_PROPOSED", "KNOWLEDGE_APPROVED"):
        audit_store.record(
            event,
            {
                "knowledge_ref": item.knowledge_ref,
                "topic": item.topic,
                "scope": item.scope,
                "audience": item.audience,
                "title": item.title,
                "content": item.content,
                "source_type": item.source_type,
            },
            actor_user_id=user.user_id,
        )

    return KnowledgeItemSummary(**item.to_dict())


@app.post(
    "/knowledge/{knowledge_ref}/supersede",
    response_model=KnowledgeItemSummary,
)
def supersede_knowledge(
    knowledge_ref: str,
    payload: KnowledgeSupersede,
    user: User = Depends(require_administer),
) -> KnowledgeItemSummary:
    """Replace an approved rule, keeping the old wording as SUPERSEDED.

    Editing approved knowledge in place would rewrite history -- a guest was
    told the old wording, and the trail has to be able to show that.
    """
    try:
        old, replacement = knowledge_store.supersede(
            knowledge_ref=knowledge_ref,
            actor_user_id=user.user_id,
            title=payload.title,
            content=payload.content,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    audit_store.record(
        "KNOWLEDGE_SUPERSEDED",
        {
            "knowledge_ref": old.knowledge_ref,
            "replaced_by": replacement.knowledge_ref,
            "topic": old.topic,
            "scope": old.scope,
            "previous_content": old.content,
            "content": replacement.content,
        },
        actor_user_id=user.user_id,
    )

    return KnowledgeItemSummary(**replacement.to_dict())


@app.get(
    "/knowledge/{knowledge_ref}",
    response_model=KnowledgeItemSummary,
)
def get_knowledge(
    knowledge_ref: str,
    user: User = Depends(require_view_runs),
) -> KnowledgeItemSummary:
    item = knowledge_store.get(knowledge_ref)

    if item is None:
        raise HTTPException(status_code=404, detail="Unknown knowledge reference.")

    return KnowledgeItemSummary(**item.to_dict())


@app.post(
    "/knowledge/{knowledge_ref}/{decision}",
    response_model=KnowledgeItemSummary,
)
def decide_knowledge(
    knowledge_ref: str,
    decision: str,
    user: User = Depends(require_administer),
) -> KnowledgeItemSummary:
    """Approve or reject a candidate. **The only path to APPROVED.**

    ADMIN-gated and always attributed: promoting a distilled guess into
    something a guest will be told is exactly the decision that needs a name
    against it. Distillation cannot reach this route.
    """
    status = KNOWLEDGE_DECISIONS.get(decision)

    if status is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown decision: {decision!r}",
        )

    try:
        item = knowledge_store.decide(
            knowledge_ref=knowledge_ref,
            status=status,
            actor_user_id=user.user_id,
        )

    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    audit_store.record(
        f"KNOWLEDGE_{status.value}",
        {
            "knowledge_ref": item.knowledge_ref,
            "topic": item.topic,
            "scope": item.scope,
            "title": item.title,
            "content": item.content,
        },
        actor_user_id=user.user_id,
    )

    return KnowledgeItemSummary(**item.to_dict())


@app.patch(
    "/knowledge/{knowledge_ref}",
    response_model=KnowledgeItemSummary,
)
def edit_knowledge(
    knowledge_ref: str,
    edit: KnowledgeEdit,
    user: User = Depends(require_administer),
) -> KnowledgeItemSummary:
    """Rewrite a rule. Deliberately does not approve it.

    An owner who edits a candidate still has to approve it, so a careless edit
    cannot promote anything on its own.
    """
    try:
        item = knowledge_store.update(
            knowledge_ref=knowledge_ref,
            actor_user_id=user.user_id,
            title=edit.title,
            content=edit.content,
            property_slug=edit.property_slug,
            scope_to_global=edit.scope_to_global,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    audit_store.record(
        "KNOWLEDGE_EDITED",
        {
            "knowledge_ref": item.knowledge_ref,
            "topic": item.topic,
            "scope": item.scope,
            "title": item.title,
            "content": item.content,
            "status": item.status,
        },
        actor_user_id=user.user_id,
    )

    return KnowledgeItemSummary(**item.to_dict())


@app.post("/webhooks/lodgify")
async def receive_lodgify_webhook(
    request: Request,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Receive one Lodgify event.

    Not model-facing and not bearer-authenticated: Lodgify calls this, and the
    HMAC signature *is* the authentication. The raw body is read before anything
    parses it, because the signature covers the exact bytes sent -- re-serialised
    JSON would not match.

    Returns 200 on anything verified, including events we do not act on:
    Lodgify retries a non-200 up to ten times, and retrying is no help when the
    problem is that we had nothing to do.
    """
    body = await request.body()

    try:
        secret = resolve_webhook_secret()

    except WebhookNotConfigured as exc:
        # Refusing beats accepting unverified events.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook verification is not configured.",
        ) from exc

    if not signature_matches(body, request.headers.get(SIGNATURE_HEADER), secret):
        # No parsing, no provider follow-up, nothing recorded.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature.",
        )

    payload = parse_webhook_body(body, request.headers.get("content-type"))

    receipt = handle_event(payload, lodgify_inbox)

    if receipt.event_type == "unparsable":
        # Schema only -- key names and types, never values. Enough to see what
        # shape actually arrived without recording a word the guest wrote.
        shape: object
        if isinstance(payload, dict):
            shape = sorted(payload)
        elif isinstance(payload, list):
            shape = [
                sorted(item) if isinstance(item, dict) else type(item).__name__
                for item in payload[:3]
            ]
        else:
            shape = type(payload).__name__

        logger.warning(
            "lodgify webhook shape: content_type=%r bytes=%d parsed=%s keys=%s",
            request.headers.get("content-type"),
            len(body),
            type(payload).__name__,
            shape,
        )

    webhook_log.record(receipt)

    # The fast path: acknowledge now, prepare the reply after responding, so
    # Lodgify never waits on a model call.
    #
    # BackgroundTasks is a local-V1 latency optimisation, NOT durable job
    # infrastructure. If this process dies between the 200 and the refresh, that
    # work is simply lost -- and correctness does not depend on it, because the
    # Inbox poll calls the very same service and will pick the conversation up
    # within its normal interval. The webhook makes drafting fast; polling makes
    # it certain.
    if receipt.resolved and receipt.conversation_ref and conversation_refresh:
        background.add_task(refresh_conversation_safely, receipt.conversation_ref)

    # Only the safe projection is returned, and never the payload.
    return {"received": True, **receipt.to_dict()}


def refresh_conversation_safely(conversation_ref: str) -> None:
    """Run a refresh outside the request, swallowing nothing silently.

    A background task has no caller to raise to, so a failure here would vanish.
    It is logged, and the conversation stays unprocessed until the next poll --
    which is exactly the recovery path this design relies on.
    """
    if conversation_refresh is None:
        return

    try:
        conversation_refresh.process(conversation_ref)

        # The webhook is why a Historic conversation is listable at all: the
        # Inbox scan will never enumerate it, so what we learned here is the
        # only record that it moved.
        if lodgify_inbox is not None:
            summary = lodgify_inbox.summarise_refs({conversation_ref}).get(
                conversation_ref
            )

            if summary is not None:
                activity_store.upsert(
                    conversation_ref=summary["conversation_ref"],
                    conversation_fingerprint=summary.get("fingerprint") or "",
                    status=summary["status"],
                    last_message_at=summary.get("last_message_at"),
                    last_message_sender=summary.get("last_message_sender"),
                    message_count=summary.get("message_count") or 0,
                    property_slug=summary.get("property_slug"),
                    source=summary.get("source"),
                    booking_status=summary.get("booking_status"),
                )

    except Exception:
        logger.exception("background refresh failed for %s", conversation_ref)


@app.get("/webhooks/lodgify/recent")
def recent_lodgify_webhooks(
    user: User = Depends(require_administer),
) -> list[dict[str, Any]]:
    """The recent verified events, for operating visibility. Ephemeral."""
    return webhook_log.recent()


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
