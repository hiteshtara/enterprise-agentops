import { useNavigate } from 'react-router-dom'
import { listRuns } from '../api/agentguard'
import type { RunStatus } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { useUrlFilter } from '../hooks/useUrlFilter'
import { formatDuration, runDurationMs } from '../lib/runStats'
import { RunStatusBadge } from '../components/Badges'
import { PageHeader } from '../components/Layout'
import { ReconcileAction } from '../components/ReconcileAction'
import { Empty, ErrorState, Loading } from '../components/States'

const STATUSES: readonly RunStatus[] = [
  'RUNNING',
  'WAITING_FOR_APPROVAL',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
]

const ACTIVE: readonly RunStatus[] = ['RUNNING', 'WAITING_FOR_APPROVAL']

const POLL_MS = 4000

function stamp(iso: string): string {
  const parsed = new Date(iso)

  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

export function RunsPage() {
  const navigate = useNavigate()

  const [status, setStatus] = useUrlFilter<RunStatus>('status', STATUSES)

  const { data, error, loading, refreshing, reload } = useAsync(
    () => listRuns({ status: status || undefined, limit: 50 }),
    [status],
    {
      intervalMs: POLL_MS,
      // Keep refreshing only while something on this page can still change.
      pollWhile: (runs) => runs.some((run) => ACTIVE.includes(run.status)),
    },
  )

  return (
    <>
      <PageHeader
        title="Runs"
        subtitle="Every agent request, durable across restarts."
        actions={
          <div className="row">
            {refreshing ? (
              <span className="faint" role="status" style={{ fontSize: 12 }}>
                <span className="spinner" /> Updating
              </span>
            ) : null}
            <button type="button" onClick={reload} aria-label="Refresh run list">
              Refresh
            </button>
          </div>
        }
      />

      <div className="field-row">
        <div className="field">
          <label className="field-label" htmlFor="run-status">
            Status
          </label>
          <select
            id="run-status"
            value={status}
            onChange={(event) => setStatus(event.target.value as RunStatus | '')}
          >
            <option value="">All statuses</option>
            {STATUSES.map((option) => (
              <option key={option} value={option}>
                {option.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </div>

        <ReconcileAction onReconciled={reload} />
      </div>

      {loading ? <Loading label="Loading runs" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data && data.length === 0 ? (
        <Empty
          message={
            status
              ? `No runs with status ${status.replace(/_/g, ' ')}.`
              : 'No runs yet. Start one from the Agent page.'
          }
        />
      ) : null}

      {data && data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Request</th>
                <th>Run ID</th>
                <th>Duration</th>
                <th>Created</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.map((run) => (
                <tr
                  key={run.run_id}
                  className="clickable"
                  onClick={() => navigate(`/runs/${run.run_id}`)}
                >
                  <td>
                    <RunStatusBadge status={run.status} />
                  </td>
                  <td className="truncate">{run.user_message}</td>
                  <td className="mono faint truncate" style={{ maxWidth: 190 }}>
                    {run.run_id}
                  </td>
                  <td className="muted mono">{formatDuration(runDurationMs(run))}</td>
                  <td className="muted">{stamp(run.created_at)}</td>
                  <td className="muted">{stamp(run.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  )
}
