from typing import Any

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    message: str = Field(min_length=1)


class ToolTrace(BaseModel):
    tool: str
    arguments: dict[str, Any]
    result: Any


class ApprovalRequest(BaseModel):
    approval_id: str
    run_id: str
    requested_by_user_id: str | None = None
    tool: str
    arguments: dict[str, Any]
    risk: str


class AgentResponse(BaseModel):
    run_id: str
    status: str
    answer: str
    trace: list[ToolTrace]
    approval_required: ApprovalRequest | None = None


class ApprovalDecision(BaseModel):
    approved: bool


class ApprovalResponse(BaseModel):
    """Resolving an approval now resumes the run, so the resumed run's outcome
    travels with the decision. The original four fields are unchanged."""

    approval_id: str
    approved: bool
    tool: str
    result: Any | None = None
    run_id: str
    run_status: str
    answer: str
    trace: list[ToolTrace] = []
    approval_required: ApprovalRequest | None = None


class AuditEvent(BaseModel):
    id: int
    run_id: str | None = None
    actor_user_id: str | None = None
    event_type: str
    details: dict[str, Any]
    created_at: str


class RunStep(BaseModel):
    step_number: int
    step_type: str
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any | None = None
    error: dict[str, Any] | None = None
    created_at: str


class RunSummary(BaseModel):
    run_id: str
    status: str
    requested_by_user_id: str | None = None
    user_message: str
    final_answer: str | None = None
    created_at: str
    updated_at: str


class RunDetail(RunSummary):
    steps: list[RunStep] = []


class ApprovalSummary(BaseModel):
    approval_id: str
    run_id: str
    requested_by_user_id: str | None = None
    resolved_by_user_id: str | None = None
    tool: str
    arguments: dict[str, Any]
    risk: str
    status: str
    created_at: str
    resolved_at: str | None = None
    decision: str | None = None


class ReconciledRun(BaseModel):
    run_id: str
    previous_status: str
    status: str
    reason: str


class ReconcileResponse(BaseModel):
    reconciled: list[ReconciledRun] = []
    count: int


class ToolSummary(BaseModel):
    name: str
    description: str
    risk: str
    parameters: dict[str, Any]


class Overview(BaseModel):
    runs_today: int
    runs_total: int
    runs_by_status: dict[str, int]
    approvals_by_status: dict[str, int]
    pending_approvals: int
    tool_executions: int
    tool_failures: int
    events_by_type: dict[str, int]
    recent_runs: list[RunSummary] = []
    recent_events: list[AuditEvent] = []


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1)


class CurrentUser(BaseModel):
    """The API-safe view of an identity. There is no password field here."""

    user_id: str
    email: str
    display_name: str
    role: str
    active: bool
    created_at: str
    permissions: list[str] = []


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: CurrentUser


class ModelExecutionSummary(BaseModel):
    sequence: int
    provider: str
    model: str | None = None
    status: str
    started_at: str
    completed_at: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    estimated_cost_usd: float | None = None
    error_type: str | None = None


class ToolExecutionSummary(BaseModel):
    tool_name: str
    status: str
    started_at: str
    completed_at: str | None = None
    duration_ms: int | None = None
    retry_number: int = 0
    arguments: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class RunMetrics(BaseModel):
    """Measured execution metrics for one run.

    Every optional field is None when the figure is genuinely unknown -- an
    unreported token count or an unpriced model is never rendered as zero.
    """

    run_id: str
    elapsed_ms: int | None = None
    active_execution_ms: int | None = None
    approval_wait_ms: int | None = None
    model_calls: int
    model_duration_ms: int | None = None
    tool_calls: int
    tool_duration_ms: int | None = None
    tool_failures: int
    tool_retries: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None
    models: list[ModelExecutionSummary] = []
    tools: list[ToolExecutionSummary] = []


class ConversationMessageSummary(BaseModel):
    """One sanitized message. Carries no provider identifier and no guest
    contact detail; `route` is deliberately absent because it cannot support a
    delivery claim (docs/LODGIFY_API.md section 12)."""

    message_ref: str
    sender: str | None = None
    subject: str | None = None
    message: str
    created_at: str | None = None
    message_status: str | None = None


class ConversationSummary(BaseModel):
    conversation_ref: str
    property_slug: str | None = None
    property_name: str | None = None
    source: str | None = None
    booking_status: str | None = None
    status: str
    last_message_at: str | None = None
    last_message_sender: str | None = None
    last_message_excerpt: str | None = None
    message_count: int


class InboxPage(BaseModel):
    conversations: list[ConversationSummary] = []
    count: int


class ConversationDetail(BaseModel):
    conversation_ref: str
    property_slug: str | None = None
    property_name: str | None = None
    source: str | None = None
    booking_status: str | None = None
    subject: str | None = None
    is_read: bool | None = None
    status: str
    messages: list[ConversationMessageSummary] = []


class GuestReplyRequest(BaseModel):
    """A reply a person composed in the console.

    The text is carried verbatim into the approval record, so what the approver
    reads is what the guest receives.
    """

    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)


class KnowledgeItemSummary(BaseModel):
    """One piece of hospitality knowledge, as the console sees it."""

    knowledge_ref: str
    property_slug: str | None = None
    scope: str
    topic: str
    title: str
    content: str
    status: str
    source_type: str
    audience: str
    safety_status: str
    safety_reasons: list[str] = []
    reason: str | None = None
    evidence_count: int
    evidence_property_count: int
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    created_at: str
    updated_at: str
    decided_at: str | None = None
    decided_by_user_id: str | None = None


class KnowledgeConflictSummary(BaseModel):
    """Approved rules that overlap. Surfaced, never resolved automatically."""

    scope: str
    topic: str
    reason: str
    message: str
    knowledge_refs: list[str] = []


class KnowledgePage(BaseModel):
    items: list[KnowledgeItemSummary] = []
    counts: dict[str, int] = {}
    conflicts: list[KnowledgeConflictSummary] = []


class KnowledgeCreate(BaseModel):
    """A rule the owner writes themselves.

    Lands APPROVED: an owner authoring a sentence by hand *is* the review, so
    asking them to approve it a moment later would be ceremony. The control that
    matters -- ADMIN only, actor recorded -- is unchanged.
    """

    property_slug: str | None = None
    topic: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    audience: str = Field(default="GUEST_FACING")


class KnowledgeSupersede(BaseModel):
    """Replacement wording for an approved rule. The old one is kept."""

    title: str | None = Field(default=None, min_length=1)
    content: str | None = Field(default=None, min_length=1)


class KnowledgeEdit(BaseModel):
    """An owner rewriting a candidate. Editing never approves it."""

    title: str | None = Field(default=None, min_length=1)
    content: str | None = Field(default=None, min_length=1)
    property_slug: str | None = None
    scope_to_global: bool = False
