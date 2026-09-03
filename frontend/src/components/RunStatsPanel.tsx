import type { RunStep, RunSummary } from '../api/types'
import { formatDuration, runStats } from '../lib/runStats'

/**
 * Counts and elapsed time derived from the run's own steps and timestamps.
 *
 * Everything here is read off durable data that already exists. There is no
 * token, cost or per-call latency measurement -- the backend does not record
 * those yet, and inventing them would make the panel a lie.
 */
export function RunStatsPanel({
  run,
  steps,
}: {
  run: Pick<RunSummary, 'created_at' | 'updated_at'>
  steps: RunStep[]
}) {
  const stats = runStats(run, steps)

  const approvalsRequested = steps.filter(
    (step) => step.step_type === 'APPROVAL_REQUIRED',
  ).length

  const approvalsResolved = steps.filter(
    (step) =>
      step.step_type === 'APPROVAL_GRANTED' || step.step_type === 'APPROVAL_DENIED',
  ).length

  const toolRequests = steps.filter(
    (step) => step.step_type === 'TOOL_REQUESTED',
  ).length

  const items: Array<{ label: string; value: string; tone?: 'danger' }> = [
    { label: 'Steps', value: String(steps.length) },
    { label: 'Model turns', value: String(stats.modelTurns) },
    { label: 'Tools requested', value: String(toolRequests) },
    { label: 'Tools executed', value: String(stats.toolExecutions) },
    {
      label: 'Tool failures',
      value: String(stats.toolFailures),
      ...(stats.toolFailures > 0 ? { tone: 'danger' as const } : {}),
    },
    { label: 'Approvals requested', value: String(approvalsRequested) },
    { label: 'Approvals resolved', value: String(approvalsResolved) },
    { label: 'Elapsed', value: formatDuration(stats.durationMs) },
  ]

  if (stats.approvalWaitMs !== null) {
    items.push({
      label: 'Approval wait',
      value: formatDuration(stats.approvalWaitMs),
    })
  }

  return (
    <section className="card" aria-label="Run statistics">
      <h2 className="card-title">
        Derived statistics
        <span className="faint" style={{ fontWeight: 400, marginLeft: 8 }}>
          — counted from this run&rsquo;s steps
        </span>
      </h2>

      <dl className="stat-strip">
        {items.map((item) => (
          <div key={item.label}>
            <dt>{item.label}</dt>
            <dd className={item.tone ? `tone-text-${item.tone}` : undefined}>
              {item.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
