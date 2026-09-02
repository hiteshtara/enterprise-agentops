import { Link, useParams } from 'react-router-dom'
import { getRun } from '../api/agentguard'
import { useAsync } from '../hooks/useAsync'
import { RunStatusBadge } from '../components/Badges'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'
import { Timeline } from '../components/Timeline'

export function RunDetailPage() {
  const { runId = '' } = useParams()
  const { data, error, loading } = useAsync(() => getRun(runId), [runId])

  return (
    <>
      <PageHeader
        title="Run detail"
        subtitle={runId}
        actions={
          <Link className="link" to="/runs">
            ← All runs
          </Link>
        }
      />

      {loading ? <Loading label="Loading run" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data ? (
        <div className="stack">
          <div className="card">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <RunStatusBadge status={data.status} />
              <Link className="link" to={`/audit?run_id=${data.run_id}`}>
                View audit for this run →
              </Link>
            </div>

            <div style={{ marginTop: 14 }}>
              <div className="approval-term">Request</div>
              <div className="prompt-echo">{data.user_message}</div>
            </div>

            {data.final_answer ? (
              <div style={{ marginTop: 14 }}>
                <div className="approval-term">Final answer</div>
                <div className="answer">{data.final_answer}</div>
              </div>
            ) : null}
          </div>

          <div>
            <div className="approval-term" style={{ marginBottom: 10 }}>
              Execution timeline
            </div>
            {data.steps.length === 0 ? (
              <Empty message="No steps recorded for this run." />
            ) : (
              <Timeline steps={data.steps} />
            )}
          </div>
        </div>
      ) : null}
    </>
  )
}
