// The single place the console talks to the backend. Components import from
// here; nothing else calls fetch().

import { query, request } from './client'
import type {
  AgentResponse,
  ConversationDetail,
  CurrentUser,
  InboxPage,
  LoginResponse,
  ApprovalResponse,
  ApprovalStatus,
  ApprovalSummary,
  AuditEvent,
  EventType,
  Overview,
  ReconcileResponse,
  RunDetail,
  RunMetrics,
  RunStatus,
  RunSummary,
  ToolSummary,
} from './types'

export function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

export function getCurrentUser(): Promise<CurrentUser> {
  return request<CurrentUser>('/auth/me')
}

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

export function listRuns(options?: {
  status?: RunStatus
  limit?: number
}): Promise<RunSummary[]> {
  return request<RunSummary[]>(
    `/runs${query({ status: options?.status, limit: options?.limit ?? 50 })}`,
  )
}

export function getRun(runId: string): Promise<RunDetail> {
  return request<RunDetail>(`/runs/${runId}`)
}

export function getRunMetrics(runId: string): Promise<RunMetrics> {
  return request<RunMetrics>(`/runs/${runId}/metrics`)
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

export function getInbox(options?: {
  propertySlug?: string
  limit?: number
}): Promise<InboxPage> {
  return request<InboxPage>(
    `/inbox${query({
      property_slug: options?.propertySlug,
      limit: options?.limit,
    })}`,
  )
}

export function getConversation(conversationRef: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/inbox/${conversationRef}`)
}

/**
 * Submits a composed reply for approval. **This sends nothing.** It creates a
 * governed run whose single pending action is `send_guest_reply`; a human still
 * has to approve it before anything reaches the guest.
 */
export function requestGuestReply(
  conversationRef: string,
  subject: string,
  message: string,
): Promise<AgentResponse> {
  return request<AgentResponse>(`/inbox/${conversationRef}/reply`, {
    method: 'POST',
    body: JSON.stringify({ subject, message }),
  })
}
