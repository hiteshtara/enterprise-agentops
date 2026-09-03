import { useState } from 'react'
import { Link } from 'react-router-dom'
import { listAuditEvents } from '../api/agentguard'
import type { EventType } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { useUrlFilter } from '../hooks/useUrlFilter'
import { EventBadge } from '../components/Badges'
import { JsonDetails } from '../components/Json'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

const EVENT_TYPES: Array<EventType | ''> = [
  '',
  'TOOL_REQUESTED',
  'TOOL_EXECUTED',
  'TOOL_FAILED',
  'APPROVAL_REQUIRED',
  'APPROVAL_GRANTED',
  'APPROVAL_DENIED',
  'AGENT_FAILED',
  'AGENT_MAX_ITERATIONS',
  'RUN_RECONCILED',
  'AUTHORIZATION_DENIED',
]

function stamp(iso: string): string {
  const parsed = new Date(iso)

  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

export function AuditPage() {
  // Both filters live in the URL, so a filtered view is refreshable, linkable,
  // and steps back and forward with the browser.
  const [runId, setRunId] = useUrlFilter('run_id')
  const [eventType, setEventType] = useUrlFilter<EventType>(
    'event_type',
    EVENT_TYPES.filter(Boolean) as EventType[],
  )

  // The run field is free text, so it is edited locally and applied on submit
  // rather than rewriting history on every keystroke.
  const [runDraft, setRunDraft] = useState(runId)

  // Adjust during render (not in an effect) when the URL changes underneath us,
  // e.g. arriving from a "View audit for this run" link or a back navigation.
  const [syncedRunId, setSyncedRunId] = useState(runId)

  if (syncedRunId !== runId) {
    setSyncedRunId(runId)
    setRunDraft(runId)
  }

  const { data, error, loading } = useAsync(
    () =>
      listAuditEvents({
        runId: runId || undefined,
        eventType: eventType || undefined,
        limit: 200,
      }),
    [runId, eventType],
  )

  return (
    <>
      <PageHeader
        title="Audit"
        subtitle="Append-only record of every governed action. Filter by run or event type."
      />

      <form
        className="field-row"
        onSubmit={(event) => {
          event.preventDefault()
          setRunId(runDraft.trim())
        }}
      >
        <div className="field" style={{ minWidth: 280 }}>
          <label className="field-label" htmlFor="run">
            Run ID
          </label>
          <input
            id="run"
            value={runDraft}
            placeholder="All runs"
            onChange={(event) => setRunDraft(event.target.value)}
          />
        </div>

        <div className="field" style={{ minWidth: 210 }}>
          <label className="field-label" htmlFor="event">
            Event type
          </label>
          <select
            id="event"
            value={eventType}
            onChange={(event) => setEventType(event.target.value as EventType | '')}
          >
            {EVENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type === '' ? 'All events' : type.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </div>

        <button type="submit">Apply filters</button>

        {runId || eventType ? (
          <button
            type="button"
            onClick={() => {
              setRunDraft('')
              setRunId('')
              setEventType('')
            }}
          >
            Clear
          </button>
        ) : null}
      </form>

      {loading ? <Loading label="Loading audit events" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data && data.length === 0 ? (
        <Empty message="No audit events match these filters." />
      ) : null}

      {data && data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Event</th>
                <th>Run</th>
                <th>Actor</th>
                <th>Tool</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {data.map((event) => (
                <tr key={event.id}>
                  <td className="muted" style={{ whiteSpace: 'nowrap' }}>
                    {stamp(event.created_at)}
                  </td>
                  <td>
                    <EventBadge type={event.event_type} />
                  </td>
                  <td>
                    {event.run_id ? (
                      <Link
                        className="link mono truncate"
                        style={{ maxWidth: 130, display: 'inline-block' }}
                        to={`/runs/${event.run_id}`}
                      >
                        {event.run_id}
                      </Link>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="mono faint truncate" style={{ maxWidth: 120 }}>
                    {event.actor_user_id ?? '—'}
                  </td>
                  <td className="mono">
                    {typeof event.details.tool === 'string' ? event.details.tool : '—'}
                  </td>
                  <td style={{ minWidth: 220 }}>
                    <JsonDetails label="Raw details" value={event.details} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  )
}
