import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  getConversation,
  requestGuestReply,
  resolveApproval,
  runAgent,
} from '../api/agentguard'
import type { AgentResponse, ApprovalRequest, SendOutcome } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { ApprovalCard } from '../components/ApprovalCard'
import { PageHeader } from '../components/Layout'
import { ErrorState, Loading } from '../components/States'
import { StatusBadge } from './InboxPage'

const DEFAULT_SUBJECT = 'Re: your message'

/**
 * The exact token a draft uses to say that no message is worth sending.
 * Mirrors `NO_REPLY_NEEDED` in app/hospitality.py by hand, like the rest of the
 * API contract in src/api/types.ts.
 */
const NO_REPLY_NEEDED = 'NO_REPLY_NEEDED'

/**
 * Drafting goes through the ordinary agent run, so it is recorded, audited and
 * measured like any other model call. The instruction is explicit that this is
 * a draft: the model has no path to send from here anyway -- `send_guest_reply`
 * would park for approval -- but asking for a draft is clearer than relying on
 * the guard to catch it.
 *
 * The prompt stays deliberately thin. How to read a conversation, when to stay
 * quiet, and what may be claimed about a property are durable business rules,
 * so they live in the hospitality knowledge layer and arrive with the
 * conversation itself -- not in a string in the browser.
 */
function draftPrompt(conversationRef: string): string {
  return (
    `Read guest conversation ${conversationRef} with get_guest_conversation, ` +
    `then follow the reply_guidance it returns exactly -- especially ` +
    `conversation_state and how_to_read_the_conversation. Reply only to what is ` +
    `still open; never re-answer something already answered. Do NOT send ` +
    `anything. Respond with the message text only -- no preamble, no subject ` +
    `line, no quotes -- or exactly ${NO_REPLY_NEEDED} if no message is worth ` +
    `sending.`
  )
}

/** Whether the model concluded that sending anything would add no value. */
function isNoReplyNeeded(answer: string): boolean {
  return answer.trim().replace(/[.\s]+$/, '') === NO_REPLY_NEEDED
}

/**
 * How many past replies informed this draft, read from the run's own trace.
 *
 * The examples themselves stay in model context and are never rendered: a
 * historical guest conversation is not something the console should surface
 * while someone writes to a different guest. The count is the useful part --
 * it tells the operator whether the draft had precedent behind it.
 */
function historicalExampleCount(trace: AgentResponse['trace']): number {
  for (const step of trace) {
    if (step.tool !== 'get_guest_conversation') continue

    const block = (step.result as { historical_examples?: { examples?: unknown[] } })
      ?.historical_examples

    if (Array.isArray(block?.examples)) return block.examples.length
  }

  return 0
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
  const [noReplyNeeded, setNoReplyNeeded] = useState(false)
  const [informedBy, setInformedBy] = useState(0)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<'draft' | 'submit' | 'decide' | null>(null)

  const detail = conversation.data

  async function generateDraft() {
    setBusy('draft')
    setError(null)
    setNoReplyNeeded(false)
    setInformedBy(0)

    try {
      const response = await runAgent(draftPrompt(conversationRef))
      const answer = response.answer ?? ''

      setInformedBy(historicalExampleCount(response.trace))

      if (isNoReplyNeeded(answer)) {
        // Concluding that nothing needs saying is a real outcome, not an empty
        // draft. Show it, leave the box empty, and touch nothing: no message is
        // composed, no approval is created, and the Lodgify thread is not
        // marked replied.
        setNoReplyNeeded(true)
        setMessage('')

        return
      }

      // Otherwise the agent's answer is a suggestion. It lands in the textarea
      // for a person to edit; nothing is sent.
      if (answer) setMessage(answer)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(null)
    }
  }

  async function submitForApproval() {
    if (!subject.trim() || !message.trim()) return

    setBusy('submit')
    setError(null)
    setOutcome(null)

    try {
      const response = await requestGuestReply(conversationRef, subject, message)

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
        conversation.reload()
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
                Suggested reply
              </div>

              {noReplyNeeded ? (
                <div className="no-reply-needed" role="status">
                  <strong>No reply needed</strong>
                  <div>
                    Everything the guest asked has been answered, and their last message
                    was a closing courtesy. Nothing has been sent and the conversation
                    has not been marked replied — write something below if you disagree.
                  </div>
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
                placeholder="Write the reply, or generate a draft to edit."
                onChange={(event) => {
                  setMessage(event.target.value)
                  setNoReplyNeeded(false)
                }}
                disabled={busy !== null}
              />

              <div
                className="row"
                style={{ marginTop: 12, justifyContent: 'space-between' }}
              >
                <span className="faint" style={{ fontSize: 12 }}>
                  {informedBy > 0
                    ? `Draft informed by ${informedBy} similar past ${
                        informedBy === 1 ? 'reply' : 'replies'
                      }. Nothing is sent until a human approves it.`
                    : 'Nothing is sent until a human approves it.'}
                </span>
                <div className="row">
                  <button
                    type="button"
                    onClick={generateDraft}
                    disabled={busy !== null}
                  >
                    {busy === 'draft' ? 'Drafting…' : 'Generate draft'}
                  </button>
                  <button
                    type="button"
                    className="primary"
                    onClick={submitForApproval}
                    disabled={busy !== null || !subject.trim() || !message.trim()}
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
