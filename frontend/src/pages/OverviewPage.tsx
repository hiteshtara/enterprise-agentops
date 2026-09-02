import { Link } from 'react-router-dom'
import { getOverview } from '../api/agentguard'
import { useAsync } from '../hooks/useAsync'
import { EventBadge, RunStatusBadge } from '../components/Badges'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

function Stat({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'warn' | 'danger' | 'ok'
}) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone && value > 0 ? ` tone-${tone}` : ''}`}>
        {value}
      </div>
    </div>
  )
}

export function OverviewPage() {
  const { data, error, loading } = useAsync(getOverview, [])

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Agent activity and governance posture across the control plane."
      />

      {loading ? <Loading label="Loading overview" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data ? (
        <div className="stack">
          <div className="grid grid-stats">
            <Stat label="Runs today" value={data.runs_today} />
            <Stat label="Completed" value={data.runs_by_status.COMPLETED} tone="ok" />
            <Stat label="Failed" value={data.runs_by_status.FAILED} tone="danger" />
            <Stat
              label="Waiting for approval"
              value={data.runs_by_status.WAITING_FOR_APPROVAL}
              tone="warn"
            />
            <Stat
              label="Pending approvals"
              value={data.pending_approvals}
              tone="warn"
            />
            <Stat label="Tool executions" value={data.tool_executions} />
            <Stat label="Tool failures" value={data.tool_failures} tone="danger" />
            <Stat label="Runs total" value={data.runs_total} />
          </div>

          <div className="grid grid-split">
            <div className="card">
              <h2 className="card-title">Recent runs</h2>
              {data.recent_runs.length === 0 ? (
                <Empty message="No runs yet. Start one from the Agent page." />
              ) : (
                <div className="stack" style={{ gap: 10 }}>
                  {data.recent_runs.map((run) => (
                    <Link
                      key={run.run_id}
                      to={`/runs/${run.run_id}`}
                      className="row"
                      style={{ justifyContent: 'space-between' }}
                    >
                      <span className="truncate" style={{ maxWidth: 260 }}>
                        {run.user_message}
                      </span>
                      <RunStatusBadge status={run.status} />
                    </Link>
                  ))}
                </div>
              )}
            </div>

            <div className="card">
              <h2 className="card-title">Recent activity</h2>
              {data.recent_events.length === 0 ? (
                <Empty message="No audit activity yet." />
              ) : (
                <div className="stack" style={{ gap: 10 }}>
                  {data.recent_events.map((event) => (
                    <div
                      key={event.id}
                      className="row"
                      style={{ justifyContent: 'space-between' }}
                    >
                      <EventBadge type={event.event_type} />
                      <span className="faint mono truncate" style={{ maxWidth: 190 }}>
                        {String(event.details.tool ?? event.run_id ?? '')}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}
