// The single place the console talks to the backend. Components import from
// here; nothing else calls fetch().

import { query, request } from './client'
import type {
  AgentResponse,
  ApprovalResponse,
  ApprovalStatus,
  ApprovalSummary,
  AuditEvent,
  EventType,
  Overview,
  ReconcileResponse,
  RunDetail,
  RunSummary,
  ToolSummary,
} from './types'

export function getOverview(): Promise<Overview> {
  return request<Overview>('/overview')
}

export function runAgent(message: string): Promise<AgentResponse> {
  return request<AgentResponse>('/agent/run', {
    method: 'POST',
    body: JSON.stringify({ message }),
  })
}

export function resolveApproval(
  approvalId: string,
  approved: boolean,
): Promise<ApprovalResponse> {
  return request<ApprovalResponse>(`/agent/approvals/${approvalId}`, {
    method: 'POST',
    body: JSON.stringify({ approved }),
  })
}

export function listApprovals(options?: {
  status?: ApprovalStatus
  runId?: string
  limit?: number
}): Promise<ApprovalSummary[]> {
  return request<ApprovalSummary[]>(
    `/approvals${query({
      status: options?.status,
      run_id: options?.runId,
      limit: options?.limit,
    })}`,
  )
}

export function listRuns(limit = 50): Promise<RunSummary[]> {
  return request<RunSummary[]>(`/runs${query({ limit })}`)
}

export function getRun(runId: string): Promise<RunDetail> {
  return request<RunDetail>(`/runs/${runId}`)
}

export function listAuditEvents(options?: {
  runId?: string
  eventType?: EventType | ''
  limit?: number
}): Promise<AuditEvent[]> {
  return request<AuditEvent[]>(
    `/audit/events${query({
      run_id: options?.runId,
      event_type: options?.eventType,
      limit: options?.limit,
    })}`,
  )
}

export function listTools(): Promise<ToolSummary[]> {
  return request<ToolSummary[]>('/tools')
}

export function reconcileRuns(staleAfterSeconds?: number): Promise<ReconcileResponse> {
  return request<ReconcileResponse>(
    `/runs/reconcile${query({ stale_after_seconds: staleAfterSeconds })}`,
    { method: 'POST' },
  )
}
