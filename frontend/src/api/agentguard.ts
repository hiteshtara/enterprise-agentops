// The single place the console talks to the backend. Components import from
// here; nothing else calls fetch().

import { query, request } from './client'
import type {
  AgentResponse,
  ConversationDetail,
  CurrentUser,
  DraftSummary,
  InboxRefreshResult,
  InboxPage,
  KnowledgeCreate,
  KnowledgeItem,
  KnowledgePage,
  KnowledgeStatus,
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

export function listKnowledge(options?: {
  status?: KnowledgeStatus
  propertySlug?: string
}): Promise<KnowledgePage> {
  return request<KnowledgePage>(
    `/knowledge${query({
      status: options?.status,
      property_slug: options?.propertySlug,
    })}`,
  )
}

export function decideKnowledge(
  knowledgeRef: string,
  decision: 'approve' | 'reject',
): Promise<KnowledgeItem> {
  return request<KnowledgeItem>(`/knowledge/${knowledgeRef}/${decision}`, {
    method: 'POST',
  })
}

/** Edits a candidate's wording. Deliberately does not approve it. */
export function editKnowledge(
  knowledgeRef: string,
  edit: { title?: string; content?: string },
): Promise<KnowledgeItem> {
  return request<KnowledgeItem>(`/knowledge/${knowledgeRef}`, {
    method: 'PATCH',
    body: JSON.stringify(edit),
  })
}

export function createKnowledge(payload: KnowledgeCreate): Promise<KnowledgeItem> {
  return request<KnowledgeItem>('/knowledge', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** Replaces an approved rule. The old wording is kept as SUPERSEDED. */
export function supersedeKnowledge(
  knowledgeRef: string,
  replacement: { title?: string; content?: string },
): Promise<KnowledgeItem> {
  return request<KnowledgeItem>(`/knowledge/${knowledgeRef}/supersede`, {
    method: 'POST',
    body: JSON.stringify(replacement),
  })
}

/** Prepares replies for conversations that need one. Sends nothing. */
export function refreshInbox(limit?: number): Promise<InboxRefreshResult> {
  return request<InboxRefreshResult>(`/inbox/refresh${query({ limit })}`, {
    method: 'POST',
  })
}

/** Saves the operator's wording. Editing never sends. */
export function editDraft(
  conversationRef: string,
  edit: { subject?: string; message?: string },
): Promise<DraftSummary> {
  return request<DraftSummary>(`/inbox/${conversationRef}/draft`, {
    method: 'PATCH',
    body: JSON.stringify(edit),
  })
}

/** Redoes the work for the conversation as it stands now. */
export function regenerateDraft(conversationRef: string): Promise<DraftSummary> {
  return request<DraftSummary>(`/inbox/${conversationRef}/draft/regenerate`, {
    method: 'POST',
  })
}
