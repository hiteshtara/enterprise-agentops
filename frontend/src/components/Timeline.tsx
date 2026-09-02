import type { RunStep } from '../api/types'
import { EventBadge } from './Badges'
import { EVENT_TONE } from './tones'
import { ArgumentList, JsonDetails } from './Json'

function time(iso: string): string {
  const parsed = new Date(iso)

  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleTimeString()
}

function summary(step: RunStep): string | null {
  if (step.step_type !== 'MODEL_RESPONSE') return null

  const result = step.result as { text?: string | null; tool_calls?: unknown[] } | null

  if (result?.text) return result.text

  const calls = result?.tool_calls?.length ?? 0

  return calls > 0 ? `Requested ${calls} tool call${calls > 1 ? 's' : ''}` : 'No output'
}

export function Timeline({ steps }: { steps: RunStep[] }) {
  return (
    <ol
      className="timeline"
      aria-label="Run timeline"
      style={{ listStyle: 'none', margin: 0, padding: 0 }}
    >
      {steps.map((step, index) => {
        const tone = EVENT_TONE[step.step_type] ?? 'neutral'
        const note = summary(step)

        return (
          <li className="timeline-row" key={step.step_number}>
            <div className="timeline-rail" aria-hidden="true">
              <span
                className={`timeline-node tone-${tone}`}
                style={{ background: 'currentColor' }}
              />
              {index < steps.length - 1 ? <span className="timeline-line" /> : null}
            </div>

            <div className="timeline-body">
              <div className="timeline-head">
                <EventBadge type={step.step_type} />
                {step.tool_name ? <span className="mono">{step.tool_name}</span> : null}
                <span className="timeline-step">
                  step {step.step_number} · {time(step.created_at)}
                </span>
              </div>

              {note ? <div className="timeline-detail muted">{note}</div> : null}

              {step.arguments ? (
                <div className="timeline-detail">
                  <ArgumentList args={step.arguments} />
                </div>
              ) : null}

              {step.error ? (
                <div className="timeline-detail">
                  <JsonDetails label="Error detail" value={step.error} />
                </div>
              ) : null}

              {step.result != null && step.step_type !== 'MODEL_RESPONSE' ? (
                <div className="timeline-detail">
                  <JsonDetails label="Result" value={step.result} />
                </div>
              ) : null}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
