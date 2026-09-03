import { getRunMetrics } from '../api/agentguard'
import type { RunMetrics } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { formatCost, formatCount, formatMs, formatTokens } from '../lib/format'
import { ErrorState, Loading } from './States'

function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'danger'
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={tone ? `tone-text-${tone}` : undefined}>{value}</dd>
      {hint ? <div className="metric-hint faint">{hint}</div> : null}
    </div>
  )
}

function models(metrics: RunMetrics): string {
  const names = [...new Set(metrics.models.map((m) => m.model).filter(Boolean))]

  return names.length > 0 ? names.join(', ') : 'Unavailable'
}

function time(iso: string): string {
  const parsed = new Date(iso)

  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleTimeString()
}

/**
 * Measured execution metrics for a run.
 *
 * Complements the step timeline rather than replacing it: the timeline says
 * what happened, this says what it cost.
 */
export function ObservabilityPanel({ runId }: { runId: string }) {
  const { data, error, loading } = useAsync(() => getRunMetrics(runId), [runId])

  return (
    <section className="card" aria-label="Observability">
      <h2 className="card-title">
        Observability
        <span className="faint" style={{ fontWeight: 400, marginLeft: 8 }}>
          — measured during execution
        </span>
      </h2>

      {loading ? <Loading label="Loading metrics" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data ? (
        <>
          <dl className="stat-strip" role="group" aria-label="Run metrics">
            <Metric label="Model" value={models(data)} />
            <Metric label="Elapsed" value={formatMs(data.elapsed_ms)} />
            <Metric
              label="Active execution"
              value={formatMs(data.active_execution_ms)}
              hint="model + tool time"
            />
            <Metric
              label="Approval wait"
              value={formatMs(data.approval_wait_ms)}
              hint="human decision time"
            />
            <Metric label="Model calls" value={formatCount(data.model_calls)} />
            <Metric label="Tool calls" value={formatCount(data.tool_calls)} />
            <Metric
              label="Tool failures"
              value={formatCount(data.tool_failures)}
              {...(data.tool_failures > 0 ? { tone: 'danger' as const } : {})}
            />
            <Metric label="Input tokens" value={formatTokens(data.input_tokens)} />
            <Metric label="Output tokens" value={formatTokens(data.output_tokens)} />
            <Metric label="Total tokens" value={formatTokens(data.total_tokens)} />
            <Metric
              label="Estimated cost"
              value={formatCost(data.estimated_cost_usd)}
              hint="estimate, not billing"
            />
          </dl>

          {data.models.length > 0 ? (
            <div className="table-wrap" style={{ marginTop: 18 }}>
              <table aria-label="Model calls">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Model</th>
                    <th>Status</th>
                    <th>Duration</th>
                    <th>Input</th>
                    <th>Output</th>
                    <th>Est. cost</th>
                  </tr>
                </thead>
                <tbody>
                  {data.models.map((call) => (
                    <tr key={call.sequence}>
                      <td className="faint">{call.sequence}</td>
                      <td className="mono">{call.model ?? 'Unavailable'}</td>
                      <td
                        className={
                          call.status === 'FAILED' ? 'tone-text-danger' : 'muted'
                        }
                      >
                        {call.status}
                        {call.error_type ? ` · ${call.error_type}` : ''}
                      </td>
                      <td className="mono">{formatMs(call.duration_ms)}</td>
                      <td className="mono">{formatTokens(call.input_tokens)}</td>
                      <td className="mono">{formatTokens(call.output_tokens)}</td>
                      <td className="mono">{formatCost(call.estimated_cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          {data.tools.length > 0 ? (
            <div className="table-wrap" style={{ marginTop: 14 }}>
              <table aria-label="Tool executions">
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>Status</th>
                    <th>Duration</th>
                    <th>Retry</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {data.tools.map((execution, index) => (
                    <tr key={`${execution.tool_name}-${index}`}>
                      <td className="mono">{execution.tool_name}</td>
                      <td
                        className={
                          execution.status === 'FAILED' ? 'tone-text-danger' : 'muted'
                        }
                      >
                        {execution.status}
                        {execution.error &&
                        typeof execution.error.error_type === 'string'
                          ? ` · ${execution.error.error_type}`
                          : ''}
                      </td>
                      <td className="mono">{formatMs(execution.duration_ms)}</td>
                      <td className="muted">
                        {execution.retry_number > 0
                          ? `retry ${execution.retry_number}`
                          : '—'}
                      </td>
                      <td className="muted">{time(execution.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  )
}
