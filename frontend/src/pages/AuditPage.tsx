import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { listAuditEvents } from '../api/agentguard'
import type { EventType } from '../api/types'
import { useAsync } from '../hooks/useAsync'
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
  const [params] = useSearchParams()

  const [runId, setRunId] = useState(params.get('run_id') ?? '')
  const [eventType, setEventType] = useState<EventType | ''>('')
  const [applied, setApplied] = useState({
    runId: params.get('run_id') ?? '',
    eventType: '' as EventType | '',
  })

  const { data, error, loading } = useAsync(
    () =>
      listAuditEvents({
        runId: applied.runId || undefined,
        eventType: applied.eventType || undefined,
        limit: 200,
      }),
    [applied.runId, applied.eventType],
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
          setApplied({ runId, eventType })
        }}
      >
        <div className="field" style={{ minWidth: 280 }}>
          <label className="field-label" htmlFor="run">
            Run ID
          </label>
          <input
            id="run"
            value={runId}
            placeholder="All runs"
            onChange={(event) => setRunId(event.target.value)}
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
