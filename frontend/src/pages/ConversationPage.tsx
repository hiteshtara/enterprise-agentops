import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  editDraft,
  getConversation,
  regenerateDraft,
  requestGuestReply,
  resolveApproval,
} from '../api/agentguard'
import type { ApprovalRequest, DraftSummary, SendOutcome } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { ApprovalCard } from '../components/ApprovalCard'
import { PageHeader } from '../components/Layout'
import { ErrorState, Loading } from '../components/States'
import { DraftBadge, StatusBadge } from './InboxPage'

const DEFAULT_SUBJECT = 'Re: your message'

const STALE_WARNING =
  'The guest has written again since this reply was prepared. Regenerate it before sending — the prepared text answers an older version of this conversation.'

/**
 * The prepared reply, if it is safe to act on.
 *
 * A draft written before the guest's latest message answers a question that has
 * moved on, so a STALE draft deliberately yields nothing here -- the operator
 * gets a warning and a Regenerate button rather than sendable text.
 *
 * NEEDS_HUMAN_REVIEW *with* text is included. That status covers two different
 * things: a reply the owner has to decide about -- a request past what policy
 * lets AgentGuard offer on its own -- and a draft that could not be written at
 * all. The first has wording the owner needs to read; the second has nothing,
 * and falls out here because there is no message.
 */
function sendableDraft(draft: DraftSummary | null | undefined) {
  if (!draft) return null

  const carriesText =
    draft.status === 'DRAFT_READY' ||
    draft.status === 'EDITED' ||
    draft.status === 'NEEDS_HUMAN_REVIEW'

  if (!carriesText) return null
  if (!draft.message) return null

  return { subject: draft.subject ?? DEFAULT_SUBJECT, message: draft.message }
}

function outcomeTone(status: SendOutcome['status']): string {
  if (status === 'confirmed_sent') return 'state-ok'
  if (status === 'confirmed_failed') return 'state-error'

  return 'state-warn'
}

export function ConversationPage() {
  const { conversationRef = '' } = useParams()

  const conversation = useAsync(
    useCallback(() => getConversation(conversationRef), [conversationRef]),
    [conversationRef],
  )

  const [subject, setSubject] = useState(DEFAULT_SUBJECT)
  const [message, setMessage] = useState('')
  const [approval, setApproval] = useState<ApprovalRequest | null>(null)
  const [outcome, setOutcome] = useState<SendOutcome | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<'regenerate' | 'save' | 'submit' | 'decide' | null>(
    null,
  )

  const detail = conversation.data
  const draft = detail?.draft ?? null
  const sendable = sendableDraft(draft)
  const isStale = draft?.status === 'STALE'
  const needsReview = draft?.status === 'NEEDS_HUMAN_REVIEW'

  // Which prepared draft the editor currently shows, keyed on the draft's own
  // identity and revision: a poll returning the same draft must never overwrite
  // what the operator is typing, while a regenerate must.
  //
  // Adjusted during render rather than in an effect, so the prepared text is
  // present in the very commit that first shows the conversation -- there is no
  // frame in which the box is empty and a keystroke could land in it.
  const revision = draft ? `${draft.draft_ref}:${draft.updated_at}` : null
  const [seeded, setSeeded] = useState<string | null>(null)

  if (revision !== null && revision !== seeded) {
    setSeeded(revision)
    setSubject(draft?.subject ?? DEFAULT_SUBJECT)
    setMessage(sendable?.message ?? '')
  }

  async function regenerate() {
    setBusy('regenerate')
    setError(null)

    try {
      await regenerateDraft(conversationRef)
      // Reload rather than trusting the returned draft: the conversation may
      // have moved on again, and the fingerprint that decides staleness is
      // computed against the thread, not against this response.
      await conversation.reload()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(null)
    }
  }

  async function saveEdit() {
    if (!message.trim()) return

    setBusy('save')
    setError(null)

    try {
      await editDraft(conversationRef, { subject, message })
      await conversation.reload()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(null)
    }
  }

  async function submitForApproval() {
    // The fingerprint is the conversation state this text was written against.
    // Submitting without one is refused by the server, so there is nothing to
    // gain by trying -- and the refusal to send here is convenience anyway: the
    // server re-checks currency and answers 409 if the guest has written since.
    const fingerprint = detail?.fingerprint

    if (!subject.trim() || !message.trim() || isStale || !fingerprint) return

    setBusy('submit')
    setError(null)
    setOutcome(null)

    try {
      const response = await requestGuestReply(
        conversationRef,
        subject,
        message,
        fingerprint,
      )

      setApproval(response.approval_required)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(null)
    }
  }

  async function decide(approved: boolean) {
    if (!approval) return

    setBusy('decide')
    setError(null)

    try {
      const response = await resolveApproval(approval.approval_id, approved)

      setApproval(null)

      if (approved && response.result) {
        setOutcome(response.result as SendOutcome)
        await conversation.reload()
      }
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <PageHeader
        title="Conversation"
        subtitle={detail?.property_name ?? undefined}
        actions={
          <Link className="link" to="/inbox">
            ← Back to Inbox
          </Link>
        }
      />

      {conversation.loading ? <Loading label="Loading conversation" /> : null}
      {conversation.error ? <ErrorState error={conversation.error} /> : null}

      {detail ? (
        <div className="stack">
          <div className="card">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <div className="row">
                <StatusBadge status={detail.status} />
                {draft ? <DraftBadge status={draft.status} /> : null}
                {detail.source ? <span className="faint">{detail.source}</span> : null}
              </div>
              <span className="faint mono" style={{ fontSize: 12 }}>
                {detail.conversation_ref}
              </span>
            </div>
          </div>

          <div>
            <div className="approval-term" style={{ marginBottom: 8 }}>
              Guest conversation
            </div>

            <div className="stack" data-testid="messages">
              {detail.messages.length === 0 ? (
                <div className="state">No messages in this conversation.</div>
              ) : null}

              {detail.messages.map((entry) => (
                <div
                  key={entry.message_ref}
                  className={
                    entry.sender === 'Owner' ? 'message message-owner' : 'message'
                  }
                >
                  <div className="row" style={{ justifyContent: 'space-between' }}>
                    <strong>{entry.sender === 'Owner' ? 'You' : 'Guest'}</strong>
                    <span className="faint mono" style={{ fontSize: 12 }}>
                      {entry.created_at ?? '—'}
                    </span>
                  </div>
                  <div className="message-body">{entry.message}</div>
                </div>
              ))}
            </div>
          </div>

          {outcome ? (
            <div className={`state ${outcomeTone(outcome.status)}`} role="status">
              {outcome.message}
            </div>
          ) : null}

          {error ? <ErrorState error={error} /> : null}

          {approval ? (
            <ApprovalCard
              tool={approval.tool}
              risk={approval.risk}
              args={approval.arguments}
              runId={approval.run_id}
              busy={busy === 'decide'}
              onDecision={decide}
              requestedBy={approval.requested_by_user_id}
              context={{ property: detail.property_name, source: detail.source }}
            />
          ) : (
            <div className="card">
              <div className="approval-term" style={{ marginBottom: 8 }}>
                {sendable ? 'Prepared reply' : 'Reply'}
              </div>

              {needsReview && sendable ? (
                <div className="state state-warn" role="status">
                  {draft?.detail}
                </div>
              ) : null}

              {isStale ? (
                <div className="state state-warn" role="status">
                  {STALE_WARNING}
                </div>
              ) : null}

              {!isStale && draft && !sendable && draft.detail ? (
                <div className="no-reply-needed" role="status">
                  <div>{draft.detail}</div>
                </div>
              ) : null}

              {!draft ? (
                <div className="state" role="status">
                  No reply has been prepared for this conversation yet.
                </div>
              ) : null}

              <label className="field-label" htmlFor="reply-subject">
                Subject
              </label>
              <input
                id="reply-subject"
                value={subject}
                onChange={(event) => setSubject(event.target.value)}
                disabled={busy !== null}
              />

              <label
                className="field-label"
                htmlFor="reply-message"
                style={{ marginTop: 12 }}
              >
                Message
              </label>
              <textarea
                id="reply-message"
                value={message}
                placeholder="Write the reply, or regenerate a draft to edit."
                onChange={(event) => setMessage(event.target.value)}
                disabled={busy !== null}
              />

              <div
                className="row"
                style={{ marginTop: 12, justifyContent: 'space-between' }}
              >
                <span className="faint" style={{ fontSize: 12 }}>
                  Nothing is sent until a human approves it.
                </span>
                <div className="row">
                  <button type="button" onClick={regenerate} disabled={busy !== null}>
                    {busy === 'regenerate' ? 'Regenerating…' : 'Regenerate'}
                  </button>
                  <button
                    type="button"
                    onClick={saveEdit}
                    disabled={busy !== null || !message.trim()}
                  >
                    {busy === 'save' ? 'Saving…' : 'Save edit'}
                  </button>
                  <button
                    type="button"
                    className="primary"
                    onClick={submitForApproval}
                    disabled={
                      busy !== null || isStale || !subject.trim() || !message.trim()
                    }
                  >
                    {busy === 'submit' ? 'Submitting…' : 'Send for approval'}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      ) : null}
    </>
  )
}
