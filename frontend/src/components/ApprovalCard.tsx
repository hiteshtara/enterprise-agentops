import { useState } from 'react'
import type { Json, ToolRisk } from '../api/types'
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
}

export function ApprovalCard({
  tool,
  risk,
  args,
  runId,
  evidence,
  onDecision,
  busy = false,
}: ApprovalCardProps) {
  const [pending, setPending] = useState<'approve' | 'reject' | null>(null)

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
        Approval required
      </div>

      <p className="page-subtitle" style={{ marginTop: 8 }}>
        AgentGuard blocked this action because the tool is classified {risk}. It has not
        been executed.
      </p>

      <div className="approval-grid">
        <div>
          <div className="approval-term">Tool</div>
          <div className="mono">{tool}</div>
        </div>
        <div>
          <div className="approval-term">Risk</div>
          <RiskBadge risk={risk} />
        </div>
        <div>
          <div className="approval-term">Arguments</div>
          <ArgumentList args={args} />
        </div>
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
        <button
          type="button"
          className="approve"
          disabled={disabled}
          onClick={() => decide(true)}
        >
          {pending === 'approve' ? 'Approving…' : 'Approve'}
        </button>
        <button
          type="button"
          className="reject"
          disabled={disabled}
          onClick={() => decide(false)}
        >
          {pending === 'reject' ? 'Rejecting…' : 'Reject'}
        </button>
      </div>
    </section>
  )
}
