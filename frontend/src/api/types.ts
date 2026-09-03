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
