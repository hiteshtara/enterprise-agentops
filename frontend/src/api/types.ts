// Mirrors the AgentGuard backend contracts in app/models.py.
// Keep these in sync by hand; a mismatch shows up as a typecheck failure here.

export type RunStatus =
  'RUNNING' | 'WAITING_FOR_APPROVAL' | 'COMPLETED' | 'FAILED' | 'CANCELLED'

export type ApprovalStatus = 'PENDING' | 'APPROVED' | 'REJECTED'

export type ToolRisk = 'READ' | 'WRITE' | 'DANGEROUS'

export type Role = 'VIEWER' | 'OPERATOR' | 'APPROVER' | 'ADMIN'

export type Permission =
  | 'VIEW_RUNS'
  | 'VIEW_AUDIT'
  | 'VIEW_TOOLS'
  | 'VIEW_APPROVALS'
  | 'RUN_AGENT'
  | 'APPROVE_WRITE'
  | 'APPROVE_DANGEROUS'
  | 'RECONCILE_RUNS'
  | 'ADMINISTER'

export interface CurrentUser {
  user_id: string
  email: string
  display_name: string
  role: Role
  active: boolean
  created_at: string
  permissions: Permission[]
}

export interface LoginResponse {
  access_token: string
  token_type: string
  user: CurrentUser
}

export type StepType =
  | 'MODEL_RESPONSE'
  | 'TOOL_REQUESTED'
  | 'TOOL_EXECUTED'
  | 'TOOL_FAILED'
  | 'APPROVAL_REQUIRED'
  | 'APPROVAL_GRANTED'
  | 'APPROVAL_DENIED'
  | 'RUN_RECONCILED'

export type EventType =
  | 'TOOL_REQUESTED'
  | 'TOOL_EXECUTED'
  | 'TOOL_FAILED'
  | 'APPROVAL_REQUIRED'
  | 'APPROVAL_GRANTED'
  | 'APPROVAL_DENIED'
  | 'AGENT_FAILED'
  | 'AGENT_MAX_ITERATIONS'
  | 'RUN_RECONCILED'
  | 'AUTHORIZATION_DENIED'

export type Json = unknown

export interface ToolTrace {
  tool: string
  arguments: Record<string, Json>
  result: Json
}

export interface ApprovalRequest {
  approval_id: string
  run_id: string
  requested_by_user_id?: string | null
  tool: string
  arguments: Record<string, Json>
  risk: ToolRisk
}

export interface AgentResponse {
  run_id: string
  status: RunStatus
  answer: string
  trace: ToolTrace[]
  approval_required: ApprovalRequest | null
}

export interface ApprovalResponse {
  approval_id: string
  approved: boolean
  tool: string
  result: Json
  run_id: string
  run_status: RunStatus
  answer: string
  trace: ToolTrace[]
  approval_required: ApprovalRequest | null
}

export interface ApprovalSummary {
  approval_id: string
  run_id: string
  requested_by_user_id: string | null
  resolved_by_user_id: string | null
  tool: string
  arguments: Record<string, Json>
  risk: ToolRisk
  status: ApprovalStatus
  created_at: string
  resolved_at: string | null
  decision: string | null
}

export interface AuditEvent {
  id: number
  run_id: string | null
  actor_user_id: string | null
  event_type: EventType
  details: Record<string, Json>
  created_at: string
}

export interface RunSummary {
  run_id: string
  status: RunStatus
  requested_by_user_id: string | null
  user_message: string
  final_answer: string | null
  created_at: string
  updated_at: string
}

export interface RunStep {
  step_number: number
  step_type: StepType
  tool_name: string | null
  arguments: Record<string, Json> | null
  result: Json
  error: Record<string, Json> | null
  created_at: string
}

export interface RunDetail extends RunSummary {
  steps: RunStep[]
}

export interface ToolSummary {
  name: string
  description: string
  risk: ToolRisk
  parameters: Record<string, Json>
}

export interface Overview {
  runs_today: number
  runs_total: number
  runs_by_status: Record<RunStatus, number>
  approvals_by_status: Record<ApprovalStatus, number>
  pending_approvals: number
  tool_executions: number
  tool_failures: number
  events_by_type: Record<string, number>
  recent_runs: RunSummary[]
  recent_events: AuditEvent[]
}

export interface ReconciledRun {
  run_id: string
  previous_status: RunStatus
  status: RunStatus
  reason: string
}

export interface ReconcileResponse {
  reconciled: ReconciledRun[]
  count: number
}

export interface ModelExecutionSummary {
  sequence: number
  provider: string
  model: string | null
  status: 'COMPLETED' | 'FAILED'
  started_at: string
  completed_at: string | null
  duration_ms: number | null
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  cached_input_tokens: number | null
  reasoning_tokens: number | null
  estimated_cost_usd: number | null
  error_type: string | null
}

export interface ToolExecutionSummary {
  tool_name: string
  status: 'COMPLETED' | 'FAILED'
  started_at: string
  completed_at: string | null
  duration_ms: number | null
  retry_number: number
  arguments: Record<string, Json> | null
  error: Record<string, Json> | null
}

/**
 * Measured execution metrics. A null field means the figure is genuinely
 * unknown -- never render it as zero.
 */
export interface RunMetrics {
  run_id: string
  elapsed_ms: number | null
  active_execution_ms: number | null
  approval_wait_ms: number | null
  model_calls: number
  model_duration_ms: number | null
  tool_calls: number
  tool_duration_ms: number | null
  tool_failures: number
  tool_retries: number
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  estimated_cost_usd: number | null
  models: ModelExecutionSummary[]
  tools: ToolExecutionSummary[]
}

export type ConversationStatus = 'needs_attention' | 'responded' | 'unknown'

export type MessageSender = 'Owner' | 'Renter'

export type MessageStatus = 'Delivered' | 'Sent' | 'Failed' | 'Unknown'

export type SendStatus = 'confirmed_sent' | 'confirmed_failed' | 'unknown_send_state'

/**
 * One sanitized message. `route` is deliberately absent from the contract: a
 * live send recorded route=null while really being delivered, so it cannot
 * support a delivery claim (docs/LODGIFY_API.md section 12).
 */
export interface ConversationMessage {
  message_ref: string
  sender: MessageSender | null
  subject: string | null
  message: string
  created_at: string | null
  message_status: MessageStatus | null
}

export interface ConversationSummary {
  conversation_ref: string
  fingerprint: string | null
  draft: DraftSummary | null
  property_slug: string | null
  property_name: string | null
  source: string | null
  booking_status: string | null
  status: ConversationStatus
  /**
   * Whether a person still has something to do here -- AgentGuard's own
   * projection, not the provider's. `status` says only who spoke last, so a
   * guest's closing acknowledgement leaves a thread `needs_attention` forever.
   * This is derived on the server from the current processing outcome and falls
   * back to `status` when there is none. Both fields travel: the provider's
   * answer is never overwritten.
   */
  operator_attention: boolean
  last_message_at: string | null
  last_message_sender: MessageSender | null
  last_message_excerpt: string | null
  preview_unavailable: boolean
  message_count: number
}

export interface InboxPage {
  conversations: ConversationSummary[]
  count: number
  /**
   * True when the list may be short: a booking page did not answer while the
   * backend was discovering conversations, or a conversation exists that the
   * activity index has not read yet. The rows that arrived were still read
   * live -- the list may be short, it is never wrong.
   */
  incomplete: boolean
  /**
   * True when the ordering behind this page came from index rows that have not
   * been refreshed recently, so a conversation that moved without a webhook may
   * not be in its right position yet. Not an error, and it hides nothing.
   */
  activity_stale: boolean
}

export interface ConversationDetail {
  conversation_ref: string
  fingerprint: string | null
  draft: DraftSummary | null
  property_slug: string | null
  property_name: string | null
  source: string | null
  booking_status: string | null
  subject: string | null
  is_read: boolean | null
  status: ConversationStatus
  /** The same server-side derivation as the Inbox row. See ConversationSummary. */
  operator_attention: boolean
  messages: ConversationMessage[]
}

export interface SentMessageSummary {
  message_ref: string
  message_status: MessageStatus | null
  created_at: string | null
}

/** The result shape `send_guest_reply` returns as a tool result. */
export interface SendOutcome {
  status: SendStatus
  conversation_ref: string
  message: string
  messages: SentMessageSummary[]
}

export type KnowledgeStatus = 'PROPOSED' | 'APPROVED' | 'REJECTED' | 'SUPERSEDED'

export type KnowledgeAudience = 'GUEST_FACING' | 'INTERNAL_OPERATION'

export type KnowledgeSafetyStatus = 'SAFE' | 'REVIEW_NUMERIC_FACT' | 'REJECT_SENSITIVE'

export interface KnowledgeItem {
  knowledge_ref: string
  property_slug: string | null
  scope: string
  topic: string
  title: string
  content: string
  status: KnowledgeStatus
  source_type: 'HISTORICAL_DISTILLATION' | 'MANUAL'
  audience: KnowledgeAudience
  safety_status: KnowledgeSafetyStatus
  safety_reasons: string[]
  reason: string | null
  evidence_count: number
  evidence_property_count: number
  first_observed_at: string | null
  last_observed_at: string | null
  created_at: string
  updated_at: string
  decided_at: string | null
  decided_by_user_id: string | null
}

/** Approved rules that overlap. Surfaced for a person; never auto-resolved. */
export interface KnowledgeConflict {
  scope: string
  topic: string
  reason: string
  message: string
  knowledge_refs: string[]
}

export interface KnowledgePage {
  items: KnowledgeItem[]
  counts: Record<KnowledgeStatus, number>
  conflicts: KnowledgeConflict[]
}

export interface KnowledgeCreate {
  property_slug: string | null
  topic: string
  title: string
  content: string
  audience: KnowledgeAudience
}

export type DraftStatus =
  | 'DRAFT_READY'
  | 'EDITED'
  | 'NO_REPLY_NEEDED'
  | 'NEEDS_HUMAN_REVIEW'
  | 'STALE'
  | 'SENT'
  | 'DISCARDED'

/**
 * A prepared reply, or the recorded decision not to prepare one.
 *
 * `status` is the effective status: a sendable draft whose conversation has
 * moved on reads STALE here even though `stored_status` does not say so.
 */
export interface DraftSummary {
  draft_ref: string
  conversation_ref: string
  property_slug: string | null
  status: DraftStatus
  stored_status: DraftStatus
  is_current: boolean
  subject: string | null
  message: string | null
  detail: string | null
  source_run_id: string | null
  created_at: string
  updated_at: string
  edited_at: string | null
  sent_at: string | null
}

export interface InboxRefreshResult {
  processed: number
  drafted: number
  skipped: number
  no_reply: number
  failed: number
}

/**
 * One open enquiry, as the console is allowed to see it.
 *
 * Safe metadata only -- `enquiry_ref` is the only handle, and it resolves
 * server-side. There is deliberately no guest name, email or phone, no numeric
 * id and no thread identifier, so nothing here can address a provider record.
 */
export interface EnquirySummary {
  enquiry_ref: string
  property_slug: string | null
  property_name: string | null
  source: string | null
  arrival: string | null
  departure: string | null
  is_replied: boolean | null
}

/**
 * A bounded page of open enquiries.
 *
 * `count` is how many rows arrived; `total` is how many open enquiries exist.
 * They differ whenever the queue is longer than the limit, which is why the
 * page shows both rather than the row count alone.
 */
export interface EnquiryPage {
  enquiries: EnquirySummary[]
  count: number
  total: number
}

/**
 * A generated enquiry reply, for an operator to read, edit and submit.
 *
 * Nothing here is stored and nothing is queued to send. `message` is null
 * whenever a draft could not be produced, and `detail` says why.
 */
export interface EnquiryReplyDraft {
  enquiry_ref: string
  subject: string | null
  message: string | null
  detail: string
}

/**
 * The result shape `send_enquiry_reply` returns as a tool result.
 *
 * The same three statuses as `SendOutcome` -- this system has three send
 * outcomes, not six -- keyed by the reference an enquiry actually has.
 * `unknown_send_state` is not a failure and is never safe to retry: the
 * message may already have reached a real person.
 */
export interface EnquirySendOutcome {
  status: SendStatus
  enquiry_ref: string
  message: string
  messages: SentMessageSummary[]
}

// -- vacancy ---------------------------------------------------------------

/**
 * One night on one property's calendar.
 *
 * `UNKNOWN` is a state in its own right and is never rendered as open: an
 * unestablished night is not inventory anyone may sell. `price` is null, never
 * zero, when the source carried no usable figure.
 */
export type NightState = 'BOOKED' | 'OPEN' | 'UNBOOKABLE' | 'BLOCKED' | 'UNKNOWN'

export interface VacancyNight {
  stay_date: string
  state: NightState
  price: number | null
  minimum_stay: number | null
  is_weekend: boolean
}

/** A run of consecutive nights in one state. */
export interface VacancyWindow {
  listing_id: string
  display_name: string
  start: string
  end: string
  nights: number
  weekend_nights: number
  priced_nights: number
  gross_value: number | null
  adr: number | null
  high_value_nights: number
  minimum_stays: number[]
  truncated: boolean
  complete_pricing: boolean
  reason?: string | null
  orphan_class?: 'one_night' | 'two_night' | 'longer' | null
  high_value?: boolean | null
  prices?: (number | null)[] | null
  rank?: number | null
  score?: number | null
  reasons: string[]
  lead_days?: number | null
}

export interface VacancyHighValueNight {
  listing_id: string
  display_name: string
  stay_date: string
  price: number
  threshold: number
  pct_above_median: number | null
  is_weekend: boolean
}

export interface VacancyAttention {
  listing_id: string
  display_name: string
  reasons: string[]
  /** True only when the property trails its market by the material margin. */
  below_market: boolean
  occupancy_gap_points: number | null
  listing_occupancy_pct: number | null
  market_occupancy_pct: number | null
  month_label: string | null
  provider_recommendations: string[]
}

export interface VacancyProperty {
  listing_id: string
  display_name: string
  currency: string
  last_refreshed_at: string | null
  nights_counted: number
  nights_missing: number
  booked_nights: number
  open_sellable_nights: number
  unbookable_nights: number
  blocked_nights: number
  unknown_nights: number
  occupancy_pct: number | null
  sellable_gross_value: number
  sellable_priced_nights: number
  unbookable_gross_value: number
  high_value_threshold: number | null
  median_price: number | null
  market_occupancy_pct: number | null
  listing_occupancy_pct: number | null
  booking_window_min_days: number | null
  booking_window_max_days: number | null
  provider_flag: string | null
  provider_recommendations: string[]
  calendar: VacancyNight[]
}

export interface VacancySummary {
  properties: number
  nights_counted: number
  nights_missing: number
  booked_nights: number
  open_sellable_nights: number
  unbookable_nights: number
  blocked_nights: number
  unknown_nights: number
  occupancy_pct: number | null
  sellable_gross_value: number
  unbookable_gross_value: number
}

/**
 * The whole board for one horizon. Only ever built from live provider data.
 */
export interface VacancyBoard {
  horizon_days: number
  start_date: string
  end_date: string
  source: string
  source_is_live: boolean
  generated_from_nights: number
  summary: VacancySummary
  properties: VacancyProperty[]
  unbookable_windows: VacancyWindow[]
  open_windows: VacancyWindow[]
  high_value_nights: VacancyHighValueNight[]
  needs_attention: VacancyAttention[]
  opportunities: VacancyWindow[]
}

/**
 * The Vacancy endpoint's reply.
 *
 * Two states, never blurred: PriceLabs is connected and `board` holds real
 * inventory, or it is not and `board` is absent. Sample inventory is never
 * substituted for a missing connection -- on screen the two are
 * indistinguishable, which makes a stand-in a lie rather than a placeholder.
 */
export interface VacancyResponse {
  configured: boolean
  message: string | null
  board: VacancyBoard | null
}

// -- pricing actions -------------------------------------------------------

export type PriceActionKind =
  | 'LOWER'
  | 'RAISE'
  | 'REMOVE_PIN'
  | 'HOLD'
  | 'KEEP_PIN'

/**
 * One proposed pricing action with the whole case for it.
 *
 * `actionable` is the only field to branch on when deciding whether a change
 * can be submitted. HOLD and KEEP_PIN are informational and never actionable.
 */
export interface PricingRecommendation {
  id: string
  listing_id: string
  slug: string
  display_name: string
  stay_date: string
  days_out: number
  action: PriceActionKind
  current_price: number | null
  proposed_price: number | null
  pct_change: number | null
  confidence: 'HIGH' | 'MEDIUM' | 'LOW'
  reason: string
  refused: string | null
  requires_human: boolean
  notes: string[]
  fingerprint: string
  actionable: boolean
  /** Why this cannot execute yet despite being actionable, or null. */
  blocked_reason: string | null
  pricelabs_minimum: number | null
  hard_floor: number | null
  normal_floor: number | null
  auto_raise_ceiling: number | null
  absolute_ceiling: number | null
  market_p25: number | null
  market_booked_median: number | null
  market_occupancy: number | null
  listing_occupancy: number | null
  demand: string | null
  pickup_7_days: number | null
  pinned_price: number | null
  last_refreshed_at: string | null
}

export interface PricingBandsOut {
  listing_id: string
  slug: string
  display_name: string
  hard_floor: number
  normal_floor: number
  auto_raise_ceiling: number
  absolute_ceiling: number
  automation_enabled: boolean
  raise_requires_human: boolean
}

export interface PricingRecommendationPage {
  generated_at: string
  horizon_days: number
  writes_enabled: boolean
  max_change_per_run: number
  recommendations: PricingRecommendation[]
  bands: PricingBandsOut[]
}

/** The shape `apply_pricing_action` returns as a tool result. */
export interface PricingOutcome {
  outcome: 'CONFIRMED_APPLIED' | 'CONFIRMED_FAILED' | 'UNKNOWN_WRITE_STATE'
  stay_date: string
  old_price?: number | null
  new_price?: number | null
  message: string
  needs_human: boolean
  refusal?: string | null
}
