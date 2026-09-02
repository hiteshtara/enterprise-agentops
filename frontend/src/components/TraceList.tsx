import type { ToolTrace } from '../api/types'
import { ArgumentList, JsonDetails } from './Json'

export function TraceList({ trace }: { trace: ToolTrace[] }) {
  if (trace.length === 0) {
    return <div className="state">No tools were executed.</div>
  }

  return (
    <div className="stack">
      {trace.map((step, index) => (
        <div className="card" key={`${step.tool}-${index}`}>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <span className="mono" style={{ fontSize: 13 }}>
              <span
                className="tone-ok"
                style={{ background: 'none', border: 0, padding: 0 }}
              >
                ✓
              </span>{' '}
              {step.tool}
            </span>
            <span className="faint" style={{ fontSize: 11 }}>
              executed
            </span>
          </div>

          <div style={{ marginTop: 10 }}>
            <div className="approval-term">Arguments</div>
            <ArgumentList args={step.arguments} />
          </div>

          <div style={{ marginTop: 10 }}>
            <JsonDetails label="Result" value={step.result} />
          </div>
        </div>
      ))}
    </div>
  )
}
