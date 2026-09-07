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


class DraftSummary(BaseModel):
    """A prepared reply, or the recorded decision not to prepare one.

    `status` is the *effective* status: a sendable draft whose conversation has
    moved on reads STALE here even though the stored row does not say so.
    """

    draft_ref: str
    conversation_ref: str
    property_slug: str | None = None
    status: str
    stored_status: str
    is_current: bool
    subject: str | None = None
    message: str | None = None
    detail: str | None = None
    source_run_id: str | None = None
    created_at: str
    updated_at: str
    edited_at: str | None = None
    sent_at: str | None = None


class DraftEdit(BaseModel):
    """An operator rewriting a prepared reply. Editing never sends."""

    subject: str | None = Field(default=None, min_length=1)
    message: str | None = Field(default=None, min_length=1)


class InboxRefreshResult(BaseModel):
    """What one polling refresh did. Counts only."""

    processed: int
    drafted: int
    skipped: int
    no_reply: int
    failed: int


class ConversationSummary(BaseModel):
    conversation_ref: str
    fingerprint: str | None = None
    draft: DraftSummary | None = None
    property_slug: str | None = None
    property_name: str | None = None
    source: str | None = None
    booking_status: str | None = None
    status: str
    # Whether a *person* still has something to do here, as opposed to who
    # spoke last. `status` is the provider's fact and stays exactly as it was;
    # this is AgentGuard's own projection, derived by
    # `app.drafts.operator_attention_for` from the current processing outcome
    # and falling back to `status` when there is none. Kept as a separate field
    # rather than folded into `status` so nothing loses the provider's answer.
    # Defaults True so a row built without it reads as it did before this
    # existed -- the badge shows, which is the safe direction.
    operator_attention: bool = True
    last_message_at: str | None = None
    last_message_sender: str | None = None
    last_message_excerpt: str | None = None
    message_count: int
    # False on a live row; True on a row we remembered but could not re-read.
    preview_unavailable: bool = False


class InboxPage(BaseModel):
    conversations: list[ConversationSummary] = []
    count: int
    # True when the list may be short: either a booking page did not answer
    # during discovery, or a conversation exists that the activity index has
    # not read yet. The rows that are here were still read live; the flag says
    # the list may be short, never that it is wrong. Defaults False so an older
    # client sees the pre-existing shape.
    incomplete: bool = False
    # True when the ordering behind this page came from index rows that have
    # not been refreshed recently, so a conversation that moved without a
    # webhook may not be in its right position yet. Not an error, and it never
    # hides a row. Defaults False for the same reason as `incomplete`.
    activity_stale: bool = False


class ConversationDetail(BaseModel):
    conversation_ref: str
    fingerprint: str | None = None
    draft: DraftSummary | None = None
    property_slug: str | None = None
    property_name: str | None = None
    source: str | None = None
    booking_status: str | None = None
    subject: str | None = None
    is_read: bool | None = None
    status: str
    # The same derivation as the Inbox row, from the same helper, so the two
    # screens cannot disagree about one conversation.
    operator_attention: bool = True
    messages: list[ConversationMessageSummary] = []


class GuestReplyRequest(BaseModel):
    """A reply a person composed in the console.

    The text is carried verbatim into the approval record, so what the approver
    reads is what the guest receives.

    `conversation_fingerprint` is the state the submitter was looking at when
    they composed this. It is required, and required of *every* submission --
    not only of a prepared draft -- because otherwise composing arbitrary text
    would be the way around the staleness check. The route refuses the
    submission when it no longer matches the live conversation: a reply written
    before the guest's latest message answers a question that has moved on, and
    that is decided on this token rather than on the wording, since a stale
    draft whose words happen to match a fresh one is still stale.

    The value is opaque here on purpose -- the route compares it, nothing parses
    it -- so the constraint is only that it is present and not blank. Rejecting
    blank at the schema boundary keeps a client that never read the conversation
    out even while the provider is unreadable and the comparison itself cannot
    judge.
    """

    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)
    conversation_fingerprint: str = Field(
        min_length=1,
        pattern=r"^\S+$",
    )


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


class EnquirySummary(BaseModel):
    """One open enquiry, as the console is allowed to see it.

    Safe metadata only. There is deliberately no guest name, email or phone,
    no numeric enquiry id and no `thread_uid`: `enquiry_ref` is the only
    handle, and it resolves server-side.
    """

    enquiry_ref: str
    property_slug: str | None = None
    property_name: str | None = None
    source: str | None = None
    arrival: str | None = None
    departure: str | None = None
    is_replied: bool | None = None


class EnquiryPage(BaseModel):
    """A bounded page of open enquiries, and how many there are in total.

    `count` is how many rows this response carries; `total` is how many open
    enquiries exist. They differ whenever the queue is longer than `limit`, and
    the console shows both -- a page that silently displayed twenty of
    forty-seven would read as the whole queue.
    """

    enquiries: list[EnquirySummary] = []
    count: int
    total: int


class EnquiryReplyRequest(BaseModel):
    """A reply to an enquiry, as the operator submits it for approval.

    The text is carried verbatim into the approval record, so what the approver
    reads is what the enquirer receives. The operator may have edited the
    generated draft, or written the whole thing themselves; this route cannot
    tell the difference and does not try to. Nothing is stored between the
    draft and this submission, so there is no draft revision to reconcile and
    no server-side copy that could disagree with what was on screen.

    There is no fingerprint field, unlike `GuestReplyRequest`. Staleness there
    is decided against a persisted draft and a live conversation fingerprint;
    an enquiry has neither, and inventing a token the console would have to
    carry would be ceremony rather than a check. What actually stands between
    this and a real person is unchanged: a DANGEROUS tool and a human approval.
    """

    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)


class EnquiryReplyDraft(BaseModel):
    """A generated enquiry reply, for an operator to read and copy.

    Nothing here is stored and nothing is queued to send. `message` is None
    whenever a draft could not be produced, and `detail` says why in words an
    operator can act on -- there is no fallback text.
    """

    enquiry_ref: str
    subject: str | None = None
    message: str | None = None
    detail: str


# -- vacancy ---------------------------------------------------------------


class VacancyNight(BaseModel):
    """One night on one property's calendar.

    `state` is `BOOKED` / `OPEN` / `UNBOOKABLE` / `BLOCKED` / `UNKNOWN`. An
    unknown night is never rendered as open, and `price` is null rather than
    zero when the source carried no usable figure.
    """

    stay_date: str
    state: str
    price: float | None = None
    minimum_stay: int | None = None
    is_weekend: bool


class VacancyWindow(BaseModel):
    """A run of consecutive nights in one state, priced where possible."""

    listing_id: str
    display_name: str
    start: str
    end: str
    nights: int
    weekend_nights: int
    priced_nights: int
    gross_value: float | None = None
    adr: float | None = None
    high_value_nights: int
    minimum_stays: list[int] = []
    truncated: bool
    complete_pricing: bool
    reason: str | None = None
    orphan_class: str | None = None
    high_value: bool | None = None
    prices: list[float | None] | None = None
    rank: int | None = None
    score: float | None = None
    reasons: list[str] = []
    lead_days: int | None = None


class VacancyHighValueNight(BaseModel):
    listing_id: str
    display_name: str
    stay_date: str
    price: float
    threshold: float
    pct_above_median: float | None = None
    is_weekend: bool


class VacancyAttention(BaseModel):
    listing_id: str
    display_name: str
    reasons: list[str]
    #: True only when the property trails its market by the material margin.
    #: A property ahead of its market has a negative gap and is False here.
    below_market: bool = False
    occupancy_gap_points: float | None = None
    listing_occupancy_pct: float | None = None
    market_occupancy_pct: float | None = None
    month_label: str | None = None
    provider_recommendations: list[str] = []


class VacancyProperty(BaseModel):
    listing_id: str
    display_name: str
    currency: str
    last_refreshed_at: str | None = None
    nights_counted: int
    nights_missing: int
    booked_nights: int
    open_sellable_nights: int
    unbookable_nights: int
    blocked_nights: int
    unknown_nights: int
    occupancy_pct: float | None = None
    sellable_gross_value: float
    sellable_priced_nights: int
    unbookable_gross_value: float
    high_value_threshold: float | None = None
    median_price: float | None = None
    market_occupancy_pct: float | None = None
    listing_occupancy_pct: float | None = None
    booking_window_min_days: int | None = None
    booking_window_max_days: int | None = None
    provider_flag: str | None = None
    provider_recommendations: list[str] = []
    calendar: list[VacancyNight]


class VacancySummary(BaseModel):
    properties: int
    nights_counted: int
    nights_missing: int
    booked_nights: int
    open_sellable_nights: int
    unbookable_nights: int
    blocked_nights: int
    unknown_nights: int
    occupancy_pct: float | None = None
    sellable_gross_value: float
    unbookable_gross_value: float


class VacancyBoard(BaseModel):
    """The whole Vacancy board for one horizon.

    Only ever built from a live provider's calendars. `source_is_live` records
    which provider produced it so a non-live board can never be mistaken for
    the account's own inventory.
    """

    horizon_days: int
    start_date: str
    end_date: str
    source: str
    source_is_live: bool
    generated_from_nights: int
    summary: VacancySummary
    properties: list[VacancyProperty]
    unbookable_windows: list[VacancyWindow]
    open_windows: list[VacancyWindow]
    high_value_nights: list[VacancyHighValueNight]
    needs_attention: list[VacancyAttention]
    opportunities: list[VacancyWindow]


class VacancyResponse(BaseModel):
    """The Vacancy endpoint's reply.

    Two states, never blurred: either PriceLabs is connected and `board` holds
    real inventory, or it is not and `board` is absent. There is no third state
    in which invented properties stand in for an unconfigured connector -- a
    demo portfolio shown in the runtime is indistinguishable from a real one to
    the person reading it, which makes it a lie rather than a placeholder.
    """

    configured: bool
    message: str | None = None
    board: VacancyBoard | None = None


# -- pricing actions -------------------------------------------------------


class PricingRecommendation(BaseModel):
    """One proposed pricing action, with the whole case for it.

    `actionable` is the only field a caller should branch on to decide whether
    a change can be submitted. HOLD and KEEP_PIN are informational and are
    never actionable.
    """

    id: str
    listing_id: str
    slug: str
    display_name: str
    stay_date: str
    days_out: int
    action: str
    current_price: float | None = None
    proposed_price: float | None = None
    pct_change: float | None = None
    confidence: str
    reason: str
    #: The same decision in the owner's terms; `reason` keeps the detail.
    plain_reason: str | None = None
    plain_action: str | None = None
    refused: str | None = None
    requires_human: bool = False
    notes: list[str] = []
    fingerprint: str
    actionable: bool
    #: Why this action cannot execute yet despite being actionable, or null.
    #: Set while a write path's provider behaviour is still unverified.
    blocked_reason: str | None = None
    pricelabs_minimum: float | None = None
    hard_floor: float | None = None
    normal_floor: float | None = None
    #: The owner dynamic floor for this exact night, and the evidence for it.
    #: Distinct from `hard_floor`: undercutting this asks a person, while the
    #: hard floor refuses outright.
    owner_floor: float | None = None
    owner_floor_basis: str | None = None
    auto_raise_ceiling: float | None = None
    absolute_ceiling: float | None = None
    market_p25: float | None = None
    market_booked_median: float | None = None
    market_occupancy: float | None = None
    listing_occupancy: float | None = None
    demand: str | None = None
    pickup_7_days: float | None = None
    pinned_price: float | None = None
    #: The provider's event label for the night, when it reports one.
    events: str | None = None
    #: This night satisfied both the history-backed LOWER rule and the
    #: market-based RAISE rule. Explanation only; it changed nothing.
    market_signal_conflict: bool = False
    #: What this property has actually converted at in this lead band, and how
    #: many bookings that rests on. Evidence, never a target price.
    historical_lead_band_adr: float | None = None
    history_sample_count: int = 0
    historical_reference_gap_dollars: float | None = None
    historical_reference_gap_pct: float | None = None
    #: Below the owner floor but above the hard floor: reviewable, never an
    #: ordinary reduction.
    below_owner_floor: bool = False
    #: Shown on every LOWER, unconditionally and never behind a fold.
    booking_com_warning: str | None = None
    #: Billed on an actual invoice for this listing's group. None means no
    #: invoice has been attached, not that the rate is zero.
    observed_commission_rate: float | None = None
    last_refreshed_at: str | None = None
    #: Whether the evidence is too old to act on. Unknown age counts as stale.
    stale: bool = False


class PricingBandsOut(BaseModel):
    listing_id: str
    slug: str
    display_name: str
    hard_floor: float
    normal_floor: float
    auto_raise_ceiling: float
    absolute_ceiling: float
    automation_enabled: bool
    raise_requires_human: bool


class PricingRecommendationPage(BaseModel):
    """Today's recommendations plus the switches that govern them."""

    generated_at: str
    horizon_days: int
    writes_enabled: bool
    #: Write actions that could execute right now. Permitted, never automatic:
    #: each still requires an individual human approval.
    unblocked_actions: list[str] = []
    max_change_per_run: float
    recommendations: list[PricingRecommendation]
    bands: list[PricingBandsOut]


class RevenueOpportunity(PricingRecommendation):
    """One RAISE worth a person's attention, with its arithmetic.

    Everything above `uplift` is the recommendation exactly as the engine
    produced it. `uplift` is `proposed_price - current_price` for this one
    night -- a price difference, never a revenue forecast.
    """

    uplift: float
    uplift_pct: float
    #: Triage only. Never affects `actionable`, `proposed_price`, or whether
    #: the action may run -- see `app.opportunity_priority`.
    priority: str
    priority_reasons: list[str] = []
    why_now: str = ""
    #: True only when the proposal sits exactly at the per-run cap *and* the
    #: market reference it aimed at is above that cap. Presentation only.
    is_change_clamped: bool = False


class RevenueOpportunitySummary(BaseModel):
    """Headline counts. `total_uplift` is the exact sum of the rows shown."""

    opportunities: int
    total_uplift: float
    high_confidence: int
    medium_confidence: int
    properties: int
    review_now: int = 0
    watch: int = 0
    low_priority: int = 0


class LowerOpportunity(PricingRecommendation):
    """One vacancy-fill reduction worth a person's attention."""

    priority: str
    priority_reasons: list[str] = []
    why_now: str = ""
    lower_flags: list[str] = []


class LowerOpportunitySummary(BaseModel):
    """Counts over the rows shown.

    `total_reduction` is the sum of the per-night price differences. It is not
    lost revenue and not expected revenue: these nights are unsold, so there is
    no revenue to lose, and lowering a price is not a booking.
    """

    opportunities: int
    review_now: int
    watch: int
    below_owner_floor: int
    market_signal_conflict: int
    total_reduction: float


class RevenueOpportunityPage(BaseModel):
    """The 60-day opportunity view. Read-only: nothing here can change a price.

    There is deliberately no action, token or approval id in this payload. The
    page that renders it is decision support, and giving it something to submit
    would make it a control surface.
    """

    generated_at: str
    horizon_days: int
    summary: RevenueOpportunitySummary
    opportunities: list[RevenueOpportunity] = []
    #: Vacancy-fill reductions, kept separate from RAISE throughout: a
    #: different question, a different priority rule, different evidence.
    lower_summary: LowerOpportunitySummary | None = None
    lower_opportunities: list[LowerOpportunity] = []


class PricingCleanupOut(BaseModel):
    """One cleanup record as the runner left it.

    `state` is what the pass may honestly claim about the row -- the terminal
    state when the write committed, `OWNERSHIP_LOST` when this process's claim
    was gone before its verdict could land. `attempted_state` keeps what it
    concluded, so a reader can see the difference rather than infer it.
    """

    id: str
    listing_id: str
    stay_date: str
    state: str
    attempted_state: str | None = None
    committed: bool = True
    detail: str


class PricingOutcomeOut(BaseModel):
    """One executed pricing action and what was observed afterwards.

    **Neither reservation id appears here, and there is no branch that adds
    one.** They are join keys into records that are personal; they exist for
    write-once correctness and idempotent reconciliation, and for nothing a
    reader needs.

    `first_*` is what was observed *after the action* and never changes.
    `current_*` is what is true now. Keeping both is what separates "never
    booked" from "booked after the action, later cancelled".

    Every outcome field is optional and `None` means **unknown** -- not yet
    reconciled, or the provider could not be read. A client must never render
    it as `false`, `0`, `$0.00` or "did not book".
    """

    id: str
    approval_id: str
    run_id: str
    cleanup_id: str | None = None
    listing_id: str
    stay_date: str
    action: str
    executed_at: str
    write_outcome: str
    price_before: float | None = None
    price_after: float | None = None
    currency: str | None = None
    days_out: int | None = None

    #: The evidence the decision rested on, as of the decision.
    history_adr: float | None = None
    history_sample_count: int | None = None
    market_p25: float | None = None
    market_booked_median: float | None = None
    demand: str | None = None
    listing_occupancy: float | None = None
    market_occupancy: float | None = None
    market_signal_conflict: bool | None = None
    hard_floor: float | None = None
    owner_floor: float | None = None
    observed_commission_rate: float | None = None

    #: The first post-action booking. Frozen once written.
    first_booked_at: str | None = None
    first_booking_lead_days: int | None = None
    #: Hours, not days: a booking 6.5 hours after a reduction must not be
    #: reported as "0 days".
    first_hours_from_action: float | None = None
    #: `rental_revenue / no_of_days` -- a stay average, never this night's
    #: rate. The UI label must say so.
    first_realized_stay_adr: float | None = None
    first_booking_channel: str | None = None

    current_booking_status: str | None = None
    current_realized_stay_adr: float | None = None
    current_booking_channel: str | None = None
    #: The *first* post-action booking later cancelled. Not "empty now" -- a
    #: rebooked night carries `True` here with a `booked` current status.
    cancelled_after_booking: bool | None = None

    last_reconciled_at: str | None = None
    reconcile_pass_count: int = 0
    #: When **we noticed**, not when it happened. The provider does not say.
    cancellation_first_observed_at: str | None = None
    #: "Routine polling stopped", never "immutable truth".
    finalized_at: str | None = None
    reopened_at: str | None = None


class PricingOutcomeListOut(BaseModel):
    """The outcomes board, with wording that resists a causal reading."""

    outcomes: list[PricingOutcomeOut] = []
    executed: int = 0
    booked_after_action: int = 0
    booked_then_cancelled: int = 0
    never_booked: int = 0
    unknown: int = 0
    #: Carried in the payload rather than left to the client, so every
    #: consumer of this endpoint states it.
    disclaimer: str = (
        "This records what happened after a pricing action, not because of "
        "one."
    )


class PricingReconcilerHealthOut(BaseModel):
    """Whether reconciliation is actually running.

    Exists to separate two things that otherwise look identical: "nothing
    booked" and "the reconciliation job stopped". A stale
    `oldest_unreconciled_age_hours` is the signal.
    """

    checked_at: str
    outcomes: int
    unreconciled: int
    oldest_unreconciled_executed_at: str | None = None
    oldest_unreconciled_age_hours: float | None = None
    last_successful_reconciliation_at: str | None = None
    finalized: int
    awaiting_first_booking: int
    booked_currently: int
    cancelled_after_booking: int
    reopened: int


class PricingCleanupRecordOut(BaseModel):
    """One stored cleanup obligation, as `pricing_cleanup.to_payload` builds it.

    Distinct from `PricingCleanupOut`, which is what one *pass* concluded about
    a row. This is the row itself. Carries no claim token and no lease: an
    operator needs to know a row is stuck, not to be handed the means to take
    it over.
    """

    id: str
    listing_id: str
    stay_date: str
    old_price: float | None = None
    new_price: float
    currency: str
    state: str
    adopted: bool
    approval_id: str | None = None
    created_at: str
    cleanup_at: str
    resolved_at: str | None = None
    resolution: str | None = None


class PricingCleanupWorkloadOut(BaseModel):
    """What cleanup owes right now, before anyone runs a pass.

    Read-only. Looking at the queue must never be a way of changing it, so
    there is no id, no listing and no date to supply, and nothing here claims,
    reconciles or ages out a row.

    `due_now` is what a pass would attempt; it is a candidate count, not a
    promise, because `claim` is still the gate. `oldest_overdue_hours` stays
    `None` when nothing is overdue -- zero would read as "current".
    """

    counted_at: str
    pending_write: int
    active: int
    due_now: int
    claimed: int
    delete_started: int
    needs_review: int
    unknown_cleanup_state: int
    oldest_overdue_at: str | None = None
    oldest_overdue_hours: float | None = None
    #: The rows automation will never resolve. A count alone would say a person
    #: is needed without saying which night.
    needs_attention: list[PricingCleanupRecordOut] = []


class PricingCleanupRunOut(BaseModel):
    """What one cleanup pass did.

    There is no request model to pair with this, and that is the point: the
    route takes no input at all, so a caller cannot name a listing or a date.
    The only work a pass can do is what the store already records as owed.
    """

    processed: int
    deleted: int
    by_state: dict[str, int] = {}
    ran_at: str
    records: list[PricingCleanupOut] = []


class PricingActionRequest(BaseModel):
    """A request to submit one recommendation for human approval.

    The fingerprint travels with it: the tool recomputes it from fresh
    PriceLabs state and refuses if anything material moved.
    """

    listing_id: str = Field(min_length=1)
    stay_date: str = Field(min_length=10)
    action: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    proposed_price: float | None = None
