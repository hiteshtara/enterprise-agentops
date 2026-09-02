import { useState } from 'react'
import { Link } from 'react-router-dom'
import { listApprovals, resolveApproval } from '../api/agentguard'
import type { ApprovalStatus } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { ApprovalStatusBadge, RiskBadge } from '../components/Badges'
import { ArgumentList } from '../components/Json'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

const FILTERS: Array<{ label: string; value: ApprovalStatus | '' }> = [
  { label: 'All', value: '' },
  { label: 'Pending', value: 'PENDING' },
  { label: 'Approved', value: 'APPROVED' },
  { label: 'Rejected', value: 'REJECTED' },
]

function stamp(iso: string | null): string {
  if (!iso) return '—'

  const parsed = new Date(iso)

  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

export function ApprovalsPage() {
  const [status, setStatus] = useState<ApprovalStatus | ''>('')
  const [acting, setActing] = useState<string | null>(null)
  const [actionError, setActionError] = useState<unknown>(null)

  const { data, error, loading, reload } = useAsync(
    () => listApprovals({ status: status || undefined, limit: 100 }),
    [status],
  )

  async function decide(approvalId: string, approved: boolean) {
    setActing(approvalId)
    setActionError(null)

    try {
      // Resolution always goes through the backend; the console never executes
      // a tool itself.
      await resolveApproval(approvalId, approved)
      reload()
    } catch (caught) {
      setActionError(caught)
    } finally {
      setActing(null)
    }
  }

  return (
    <>
      <PageHeader
        title="Approvals"
        subtitle="Human decisions on actions the agent proposed but could not execute."
      />

      <div className="field-row">
        <div className="field">
          <label className="field-label" htmlFor="status">
            Status
          </label>
          <select
            id="status"
            value={status}
            onChange={(event) => setStatus(event.target.value as ApprovalStatus | '')}
          >
            {FILTERS.map((filter) => (
              <option key={filter.value} value={filter.value}>
                {filter.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {loading ? <Loading label="Loading approvals" /> : null}
      {error ? <ErrorState error={error} /> : null}
      {actionError ? <ErrorState error={actionError} /> : null}

      {data && data.length === 0 ? <Empty message="No approvals to show." /> : null}

      {data && data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Tool</th>
                <th>Risk</th>
                <th>Arguments</th>
                <th>Run</th>
                <th>Requested</th>
                <th>Resolved</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((approval) => (
                <tr key={approval.approval_id}>
                  <td>
                    <ApprovalStatusBadge status={approval.status} />
                  </td>
                  <td className="mono">{approval.tool}</td>
                  <td>
                    <RiskBadge risk={approval.risk} />
                  </td>
                  <td>
                    <ArgumentList args={approval.arguments} />
                  </td>
                  <td>
                    <Link
                      className="link mono truncate"
                      style={{ maxWidth: 130, display: 'inline-block' }}
                      to={`/runs/${approval.run_id}`}
                    >
                      {approval.run_id}
                    </Link>
                  </td>
                  <td className="muted">{stamp(approval.created_at)}</td>
                  <td className="muted">{stamp(approval.resolved_at)}</td>
                  <td>
                    {approval.status === 'PENDING' ? (
                      <div className="row" style={{ gap: 6, flexWrap: 'nowrap' }}>
                        <button
                          type="button"
                          className="approve"
                          disabled={acting === approval.approval_id}
                          onClick={() => decide(approval.approval_id, true)}
                        >
                          Approve
                        </button>
                        <button
                          type="button"
                          className="reject"
                          disabled={acting === approval.approval_id}
                          onClick={() => decide(approval.approval_id, false)}
                        >
                          Reject
                        </button>
                      </div>
                    ) : (
                      // The status badge already states the decision; repeating
                      // it here would be noise.
                      <span className="faint">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  )
}
