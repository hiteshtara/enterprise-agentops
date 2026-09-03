import type { RunStep } from '../api/types'

/**
 * Counts read off the run's timeline.
 *
 * Deliberately narrow: anything Observability *measures* (elapsed, approval
 * wait, tool durations, failures) lives there instead. This panel only reports
 * what the step history alone can say, so no figure appears twice with two
 * different provenances.
 */
export function RunStatsPanel({ steps }: { steps: RunStep[] }) {
  const count = (type: RunStep['step_type']) =>
    steps.filter((step) => step.step_type === type).length

  const items = [
    { label: 'Steps', value: steps.length },
    { label: 'Model responses', value: count('MODEL_RESPONSE') },
    { label: 'Tools requested', value: count('TOOL_REQUESTED') },
    { label: 'Approvals requested', value: count('APPROVAL_REQUIRED') },
    {
      label: 'Approvals resolved',
      value: count('APPROVAL_GRANTED') + count('APPROVAL_DENIED'),
    },
  ]

  return (
    <section className="card" aria-label="Timeline counts">
      <h2 className="card-title">
        Timeline counts
        <span className="faint" style={{ fontWeight: 400, marginLeft: 8 }}>
          — counted from this run&rsquo;s recorded steps
        </span>
      </h2>

      <dl className="stat-strip" role="group" aria-label="Timeline metrics">
        {items.map((item) => (
          <div key={item.label}>
            <dt>{item.label}</dt>
            <dd>{item.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
