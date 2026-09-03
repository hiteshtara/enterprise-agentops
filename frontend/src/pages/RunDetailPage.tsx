import { Link, useParams } from 'react-router-dom'
import { getRun } from '../api/agentguard'
import type { RunDetail } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { RunStatusBadge } from '../components/Badges'
import { CopyLinkButton } from '../components/CopyLinkButton'
import { PageHeader } from '../components/Layout'
import { RunStatsPanel } from '../components/RunStatsPanel'
import { Empty, ErrorState, Loading } from '../components/States'
import { Timeline } from '../components/Timeline'

const POLL_MS = 3000

const ACTIVE = new Set(['RUNNING', 'WAITING_FOR_APPROVAL'])

export function RunDetailPage() {
  const { runId = '' } = useParams()

  const { data, error, loading, refreshing, reload } = useAsync<RunDetail>(
    () => getRun(runId),
    [runId],
    {
      intervalMs: POLL_MS,
      // Stops on its own once the run reaches a terminal status.
      pollWhile: (run) => ACTIVE.has(run.status),
    },
  )

  const failed = data?.status === 'FAILED'

  return (
    <>
      <PageHeader
        title="Run detail"
        subtitle={runId}
        actions={
          <div className="row">
            {refreshing ? (
              <span className="faint" role="status" style={{ fontSize: 12 }}>
                <span className="spinner" /> Updating
              </span>
            ) : null}
            <CopyLinkButton />
            <button type="button" onClick={reload} aria-label="Refresh this run">
              Refresh
            </button>
            <Link className="link" to="/runs">
              ← All runs
            </Link>
          </div>
        }
      />

      {loading ? <Loading label="Loading run" /> : null}
      {error && !data ? <ErrorState error={error} /> : null}

      {data ? (
        <div className="stack">
          <div className="card">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <div className="row">
                <RunStatusBadge status={data.status} />
                {ACTIVE.has(data.status) ? (
                  <span className="faint" style={{ fontSize: 12 }}>
                    live — updating automatically
                  </span>
                ) : null}
              </div>
              <Link className="link" to={`/audit?run_id=${data.run_id}`}>
                View audit for this run →
              </Link>
            </div>

            <div style={{ marginTop: 14 }}>
              <div className="approval-term">Request</div>
              <div className="prompt-echo">{data.user_message}</div>
            </div>

            <div style={{ marginTop: 14 }}>
              <div className="approval-term">Requested by</div>
              <div className="mono faint">{data.requested_by_user_id ?? '—'}</div>
            </div>

            {data.final_answer ? (
              <div style={{ marginTop: 14 }}>
                <div className="approval-term">
                  {failed ? 'Failure reason' : 'Final answer'}
                </div>
                <div className={failed ? 'answer answer-failed' : 'answer'}>
                  {data.final_answer}
                </div>
              </div>
            ) : null}
          </div>

          <RunStatsPanel run={data} steps={data.steps} />

          <div>
            <div className="approval-term" style={{ marginBottom: 10 }}>
              Execution timeline
            </div>
            {data.steps.length === 0 ? (
              <Empty message="No steps recorded for this run yet." />
            ) : (
              <Timeline steps={data.steps} />
            )}
          </div>
        </div>
      ) : null}
    </>
  )
}
