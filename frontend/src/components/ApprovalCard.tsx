import { useState } from 'react'
import { useAuth } from '../auth/context'
import type { Json, Permission, ToolRisk } from '../api/types'
import { ArgumentList } from './Json'
import { RiskBadge } from './Badges'

export interface ApprovalCardProps {
  tool: string
  risk: ToolRisk
  args: Record<string, Json> | null
  runId?: string
  evidence?: React.ReactNode
  onDecision: (approved: boolean) => Promise<void> | void
  busy?: boolean
  /** Extra context for a guest message: property and channel. */
  context?: { property?: string | null; source?: string | null }
  requestedBy?: string | null
}

export const SEND_GUEST_REPLY = 'send_guest_reply'

export const SEND_ENQUIRY_REPLY = 'send_enquiry_reply'

/**
 * The two tools that put text in front of a real person outside the business.
 *
 * They are rendered the same way for the same reason: an approver has to read
 * the exact characters that will be transmitted, and `ArgumentList` would show
 * them JSON-escaped on one line. The only difference is what the thread is
 * called -- a booked guest's conversation, or an enquiry.
 */
const MESSAGE_SENDS: Record<string, { heading: string; recipient: string }> = {
  [SEND_GUEST_REPLY]: {
    heading: 'Send guest message',
    recipient: 'a real guest',
  },
  [SEND_ENQUIRY_REPLY]: {
    heading: 'Send enquiry reply',
    recipient: 'the person who enquired',
  },
}

function text(value: Json): string {
  return typeof value === 'string' ? value : ''
}

/**
 * The outbound message, in full.
 *
 * Rendered verbatim and never truncated, collapsed or JSON-escaped: the
 * approver has to be able to read the exact characters the guest will receive,
 * because that string is transmitted byte for byte once approved.
 */
function GuestMessagePreview({
  args,
  context,
}: {
  args: Record<string, Json> | null
  context?: { property?: string | null; source?: string | null }
}) {
  return (
    <div className="guest-send">
      <div className="approval-grid">
        {context?.property ? (
          <div>
            <div className="approval-term">Property</div>
            <div>{context.property}</div>
          </div>
        ) : null}
        <div>
          <div className="approval-term">
            {args?.enquiry_ref ? 'Enquiry' : 'Conversation'}
          </div>
          <div className="mono truncate">
            {text(args?.conversation_ref) || text(args?.enquiry_ref)}
          </div>
        </div>
        {context?.source ? (
          <div>
            <div className="approval-term">Channel</div>
            <div>{context.source}</div>
          </div>
        ) : null}
      </div>

      <div className="approval-term">Subject</div>
      <div className="guest-send-subject">{text(args?.subject)}</div>

      <div className="approval-term">Exact message</div>
      <div className="guest-send-body">{text(args?.message)}</div>
    </div>
  )
}

const RISK_PERMISSION: Record<ToolRisk, Permission> = {
  READ: 'VIEW_APPROVALS',
  WRITE: 'APPROVE_WRITE',
  DANGEROUS: 'APPROVE_DANGEROUS',
}

export function ApprovalCard({
  tool,
  risk,
  args,
  runId,
  evidence,
  onDecision,
  busy = false,
  context,
  requestedBy,
}: ApprovalCardProps) {
  const send = MESSAGE_SENDS[tool]
  const guestSend = send !== undefined
  const [pending, setPending] = useState<'approve' | 'reject' | null>(null)
  const { can } = useAuth()

  // Mirrors the backend's risk-to-permission policy so the console does not
  // offer an action that would be refused. The backend still decides.
  const permitted = can(RISK_PERMISSION[risk] ?? 'APPROVE_DANGEROUS')

  const disabled = busy || pending !== null

  async function decide(approved: boolean) {
    setPending(approved ? 'approve' : 'reject')

    try {
      await onDecision(approved)
    } finally {
      setPending(null)
    }
  }

  return (
    <section className="approval-card" aria-label="Approval required">
      <div className="approval-heading">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M12 8.5v4.2M12 16.2h.01M10.3 3.9 2.6 17.4A1.9 1.9 0 0 0 4.3 20.3h15.4a1.9 1.9 0 0 0 1.7-2.9L13.7 3.9a1.9 1.9 0 0 0-3.4 0Z"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        {send ? send.heading : 'Approval required'}
      </div>

      <p className="page-subtitle" style={{ marginTop: 8 }}>
        {send
          ? `This message has not been sent. Once approved it goes to ${send.recipient}, exactly as written below, and cannot be edited or recalled.`
          : `AgentGuard blocked this action because the tool is classified ${risk}. It has not been executed.`}
      </p>

      {guestSend ? <GuestMessagePreview args={args} context={context} /> : null}

      <div className="approval-grid">
        {guestSend ? null : (
          <>
            <div>
              <div className="approval-term">Tool</div>
              <div className="mono">{tool}</div>
            </div>
            <div>
              <div className="approval-term">Arguments</div>
              <ArgumentList args={args} />
            </div>
          </>
        )}
        <div>
          <div className="approval-term">Risk</div>
          <RiskBadge risk={risk} />
        </div>
        {requestedBy ? (
          <div>
            <div className="approval-term">Requested by</div>
            <div className="mono truncate">{requestedBy}</div>
          </div>
        ) : null}
        {runId ? (
          <div>
            <div className="approval-term">Run</div>
            <div className="mono truncate">{runId}</div>
          </div>
        ) : null}
      </div>

      {evidence ? (
        <div style={{ marginBottom: 4 }}>
          <div className="approval-term">Evidence</div>
          {evidence}
        </div>
      ) : null}

      <div className="approval-actions">
        {permitted ? (
          <>
            <button
              type="button"
              className="approve"
              disabled={disabled}
              onClick={() => decide(true)}
            >
              {pending === 'approve'
                ? guestSend
                  ? 'Sending…'
                  : 'Approving…'
                : guestSend
                  ? 'Approve & Send'
                  : 'Approve'}
            </button>
            <button
              type="button"
              className="reject"
              disabled={disabled}
              onClick={() => decide(false)}
            >
              {pending === 'reject' ? 'Rejecting…' : 'Reject'}
            </button>
          </>
        ) : (
          <p className="not-permitted">
            Your role cannot decide a {risk} approval. It requires the{' '}
            <span className="mono">{RISK_PERMISSION[risk]}</span> permission.
          </p>
        )}
      </div>
    </section>
  )
}
