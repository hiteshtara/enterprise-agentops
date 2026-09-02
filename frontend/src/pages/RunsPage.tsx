import { useNavigate } from 'react-router-dom'
import { listRuns } from '../api/agentguard'
import { useAsync } from '../hooks/useAsync'
import { RunStatusBadge } from '../components/Badges'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

function stamp(iso: string): string {
  const parsed = new Date(iso)

  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

export function RunsPage() {
  const navigate = useNavigate()
  const { data, error, loading } = useAsync(() => listRuns(50), [])

  return (
    <>
      <PageHeader
        title="Runs"
        subtitle="Every agent request, durable across restarts."
      />

      {loading ? <Loading label="Loading runs" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data && data.length === 0 ? (
        <Empty message="No runs yet. Start one from the Agent page." />
      ) : null}

      {data && data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Request</th>
                <th>Run ID</th>
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
