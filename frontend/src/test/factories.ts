import type {
  AgentResponse,
  CurrentUser,
  ApprovalResponse,
  ApprovalSummary,
  AuditEvent,
  Overview,
  RunDetail,
  RunSummary,
  ToolSummary,
} from '../api/types'

export const RUN_ID = 'run-abc-123'
export const APPROVAL_ID = 'appr-xyz-789'
export const OPERATOR_ID = 'user-operator-1'
export const APPROVER_ID = 'user-approver-1'

export const failedBatchRows = [
  {
    batch_id: 43,
    status: 'FAILED',
    records: 495,
    duration_seconds: 12,
    error: 'Oracle connection timeout',
    created_at: '2026-03-14T05:00:00+00:00',
  },
]

export const waitingResponse: AgentResponse = {
  run_id: RUN_ID,
  status: 'WAITING_FOR_APPROVAL',
  answer: 'Approval required before executing restart_migration.',
  trace: [
    {
      tool: 'query_migration_batches',
      arguments: { status: 'FAILED', limit: 5 },
      result: failedBatchRows,
    },
  ],
  approval_required: {
    approval_id: APPROVAL_ID,
    run_id: RUN_ID,
    requested_by_user_id: OPERATOR_ID,
    tool: 'restart_migration',
    arguments: { batch_id: 43 },
    risk: 'WRITE',
  },
}

export const completedResponse: AgentResponse = {
  run_id: RUN_ID,
  status: 'COMPLETED',
  answer: 'Batch 43 failed because of an Oracle connection timeout.',
  trace: [
    {
      tool: 'query_migration_batches',
      arguments: { status: 'FAILED', limit: 5 },
      result: failedBatchRows,
    },
  ],
  approval_required: null,
}

export const resumedResponse: ApprovalResponse = {
  approval_id: APPROVAL_ID,
  approved: true,
  tool: 'restart_migration',
  result: { batch_id: 43, status: 'RESTARTED' },
  run_id: RUN_ID,
  run_status: 'COMPLETED',
  answer:
    'Batch 43 failed because of an Oracle connection timeout. The approved restart was executed successfully.',
  trace: [
    {
      tool: 'restart_migration',
      arguments: { batch_id: 43 },
      result: { batch_id: 43, status: 'RESTARTED' },
    },
  ],
  approval_required: null,
}

export const rejectedResponse: ApprovalResponse = {
  approval_id: APPROVAL_ID,
  approved: false,
  tool: 'restart_migration',
  result: null,
  run_id: RUN_ID,
  run_status: 'CANCELLED',
  answer: 'The requested action was not approved, so nothing was executed.',
  trace: [],
  approval_required: null,
}

export const runSummaries: RunSummary[] = [
  {
    run_id: RUN_ID,
    status: 'COMPLETED',
    requested_by_user_id: OPERATOR_ID,
    user_message: 'Investigate migration batch 43 and restart it if needed.',
    final_answer: 'Batch 43 was restarted.',
    created_at: '2026-09-02T10:00:00+00:00',
    updated_at: '2026-09-02T10:02:00+00:00',
  },
  {
    run_id: 'run-def-456',
    status: 'WAITING_FOR_APPROVAL',
    requested_by_user_id: OPERATOR_ID,
    user_message: 'Restart batch 51.',
    final_answer: null,
    created_at: '2026-09-02T11:00:00+00:00',
    updated_at: '2026-09-02T11:00:30+00:00',
  },
]

export const runDetail: RunDetail = {
  ...runSummaries[0],
  steps: [
    {
      step_number: 1,
      step_type: 'MODEL_RESPONSE',
      tool_name: null,
      arguments: null,
      result: { text: null, tool_calls: [{ name: 'query_migration_batches' }] },
      error: null,
      created_at: '2026-09-02T10:00:01+00:00',
    },
    {
      step_number: 2,
      step_type: 'TOOL_REQUESTED',
      tool_name: 'query_migration_batches',
      arguments: { status: 'FAILED', limit: 5 },
      result: null,
      error: null,
      created_at: '2026-09-02T10:00:02+00:00',
    },
    {
      step_number: 3,
      step_type: 'TOOL_EXECUTED',
      tool_name: 'query_migration_batches',
      arguments: { status: 'FAILED', limit: 5 },
      result: failedBatchRows,
      error: null,
      created_at: '2026-09-02T10:00:03+00:00',
    },
    {
      step_number: 4,
      step_type: 'APPROVAL_REQUIRED',
      tool_name: 'restart_migration',
      arguments: { batch_id: 43 },
      result: null,
      error: null,
      created_at: '2026-09-02T10:00:05+00:00',
    },
    {
      step_number: 5,
      step_type: 'APPROVAL_GRANTED',
      tool_name: 'restart_migration',
      arguments: { batch_id: 43 },
      result: null,
      error: null,
      created_at: '2026-09-02T10:01:50+00:00',
    },
    {
      step_number: 6,
      step_type: 'TOOL_EXECUTED',
      tool_name: 'restart_migration',
      arguments: { batch_id: 43 },
      result: { batch_id: 43, status: 'RESTARTED' },
      error: null,
      created_at: '2026-09-02T10:01:51+00:00',
    },
  ],
}

export const approvals: ApprovalSummary[] = [
  {
    approval_id: APPROVAL_ID,
    run_id: RUN_ID,
    requested_by_user_id: OPERATOR_ID,
    resolved_by_user_id: null,
    tool: 'restart_migration',
    arguments: { batch_id: 43 },
    risk: 'WRITE',
    status: 'PENDING',
    created_at: '2026-09-02T10:00:05+00:00',
    resolved_at: null,
    decision: null,
  },
  {
    approval_id: 'appr-old-111',
    run_id: 'run-old-999',
    requested_by_user_id: OPERATOR_ID,
    resolved_by_user_id: APPROVER_ID,
    tool: 'restart_migration',
    arguments: { batch_id: 41 },
    risk: 'WRITE',
    status: 'REJECTED',
    created_at: '2026-09-01T09:00:00+00:00',
    resolved_at: '2026-09-01T09:05:00+00:00',
    decision: 'REJECTED',
  },
]

export const auditEvents: AuditEvent[] = [
  {
    id: 4,
    run_id: RUN_ID,
    actor_user_id: APPROVER_ID,
    event_type: 'APPROVAL_GRANTED',
    details: { tool: 'restart_migration', arguments: { batch_id: 43 } },
    created_at: '2026-09-02T10:01:50+00:00',
  },
  {
    id: 3,
    run_id: RUN_ID,
    actor_user_id: OPERATOR_ID,
    event_type: 'TOOL_FAILED',
    details: { tool: 'query_migration_batches', error_type: 'ValueError' },
    created_at: '2026-09-02T10:00:04+00:00',
  },
]

export const tools: ToolSummary[] = [
  {
    name: 'query_migration_batches',
    description: 'Query the authoritative migration batch database.',
    risk: 'READ',
    parameters: { type: 'object', properties: { limit: { type: 'integer' } } },
  },
  {
    name: 'restart_migration',
    description: 'Restart a failed migration batch.',
    risk: 'WRITE',
    parameters: { type: 'object', properties: { batch_id: { type: 'integer' } } },
  },
  {
    name: 'delete_resource',
    description: 'Permanently delete a resource.',
    risk: 'DANGEROUS',
    parameters: { type: 'object', properties: {} },
  },
]

export const overview: Overview = {
  runs_today: 2,
  runs_total: 5,
  runs_by_status: {
    RUNNING: 0,
    WAITING_FOR_APPROVAL: 1,
    COMPLETED: 3,
    FAILED: 1,
    CANCELLED: 0,
  },
  approvals_by_status: { PENDING: 1, APPROVED: 2, REJECTED: 1 },
  pending_approvals: 1,
  tool_executions: 9,
  tool_failures: 2,
  events_by_type: { TOOL_EXECUTED: 9, TOOL_FAILED: 2 },
  recent_runs: runSummaries,
  recent_events: auditEvents,
}

export const operatorUser: CurrentUser = {
  user_id: OPERATOR_ID,
  email: 'operator@agentguard.local',
  display_name: 'Ola Operator',
  role: 'OPERATOR',
  active: true,
  created_at: '2026-09-01T00:00:00+00:00',
  permissions: ['VIEW_RUNS', 'VIEW_AUDIT', 'VIEW_TOOLS', 'VIEW_APPROVALS', 'RUN_AGENT'],
}

export const approverUser: CurrentUser = {
  user_id: APPROVER_ID,
  email: 'approver@agentguard.local',
  display_name: 'Ada Approver',
  role: 'APPROVER',
  active: true,
  created_at: '2026-09-01T00:00:00+00:00',
  permissions: [
    'VIEW_RUNS',
    'VIEW_AUDIT',
    'VIEW_TOOLS',
    'VIEW_APPROVALS',
    'RUN_AGENT',
    'APPROVE_WRITE',
  ],
}

export const viewerUser: CurrentUser = {
  user_id: 'user-viewer-1',
  email: 'viewer@agentguard.local',
  display_name: 'Val Viewer',
  role: 'VIEWER',
  active: true,
  created_at: '2026-09-01T00:00:00+00:00',
  permissions: ['VIEW_RUNS', 'VIEW_AUDIT', 'VIEW_TOOLS', 'VIEW_APPROVALS'],
}

export const adminUser: CurrentUser = {
  user_id: 'user-admin-1',
  email: 'admin@agentguard.local',
  display_name: 'Avi Admin',
  role: 'ADMIN',
  active: true,
  created_at: '2026-09-01T00:00:00+00:00',
  permissions: [
    'VIEW_RUNS',
    'VIEW_AUDIT',
    'VIEW_TOOLS',
    'VIEW_APPROVALS',
    'RUN_AGENT',
    'APPROVE_WRITE',
    'APPROVE_DANGEROUS',
    'RECONCILE_RUNS',
    'ADMINISTER',
  ],
}

/** What GET /runs/{id} returns after the approval is granted and the run resumes. */
export const completedRunDetail: RunDetail = {
  ...runDetail,
  status: 'COMPLETED',
  final_answer:
    'Batch 43 failed because of an Oracle connection timeout. The approved restart was executed successfully.',
}

/** What GET /runs/{id} returns after the approval is rejected. */
export const cancelledRunDetail: RunDetail = {
  ...runDetail,
  status: 'CANCELLED',
  final_answer: 'The requested action was not approved, so nothing was executed.',
  steps: runDetail.steps.slice(0, 4),
}

/** A run parked awaiting a decision, used for restore-from-URL tests. */
export const waitingRunDetail: RunDetail = {
  ...runDetail,
  status: 'WAITING_FOR_APPROVAL',
  final_answer: null,
  steps: runDetail.steps.slice(0, 4),
}
