import { Link } from 'react-router-dom'
import { getInbox } from '../api/agentguard'
import type { ConversationStatus } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

/**
 * V1's entire "notice a new guest message" mechanism.
 *
 * The console polls while the page is open; `useAsync` skips hidden tabs and
 * clears the timer on unmount, so nothing calls Lodgify when nobody is looking
 * at the Inbox. No webhook, no background worker, no process to supervise.
 */
const POLL_MS = 30_000

const STATUS_LABEL: Record<ConversationStatus, string> = {
  needs_attention: 'Needs attention',
  responded: 'Responded',
  unknown: 'Unknown',
}

const STATUS_TONE: Record<ConversationStatus, string> = {
  needs_attention: 'tone-warn',
  responded: 'tone-ok',
  // Unknown is neutral on purpose. Showing it as a warning would train the
  // operator to ignore the one badge that means "a guest is waiting".
  unknown: 'tone-neutral',
}

export function StatusBadge({ status }: { status: ConversationStatus }) {
  return (
    <span className={`badge ${STATUS_TONE[status] ?? 'tone-neutral'}`}>
      <span className="badge-dot" aria-hidden="true" />
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

export function InboxPage() {
  const inbox = useAsync(() => getInbox({ limit: 20 }), [], {
    intervalMs: POLL_MS,
    // Always poll while the page is mounted and visible; there is no terminal
    // state for an inbox.
    pollWhile: () => true,
  })

  const conversations = inbox.data?.conversations ?? []

  return (
    <>
      <PageHeader
        title="Inbox"
        subtitle="Guest conversations from the booking provider. Replies are drafted here and sent only after approval."
        actions={
          <div className="row">
            {inbox.refreshing ? (
              <span className="faint" style={{ fontSize: 12 }}>
                Refreshing…
              </span>
            ) : null}
            <button type="button" onClick={inbox.reload} disabled={inbox.loading}>
              Refresh
            </button>
          </div>
        }
      />

      {inbox.error && !inbox.data ? <ErrorState error={inbox.error} /> : null}

      {inbox.loading ? <Loading label="Loading conversations" /> : null}

      {!inbox.loading && conversations.length === 0 && !inbox.error ? (
        <Empty message="No recent guest conversations." />
      ) : null}

      {conversations.length > 0 ? (
        <div className="stack">
          {conversations.map((row) => (
            <Link
              key={row.conversation_ref}
              to={`/inbox/${row.conversation_ref}`}
              className="card conversation-row"
            >
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <div className="row">
                  <StatusBadge status={row.status} />
                  <strong>{row.property_name ?? 'Unmapped property'}</strong>
                  {row.source ? <span className="faint">{row.source}</span> : null}
                </div>
                <span className="faint mono" style={{ fontSize: 12 }}>
                  {row.last_message_at ?? '—'}
                </span>
              </div>

              <div className="conversation-excerpt">
                {row.last_message_excerpt ? (
                  <>
                    <span className="faint">
                      {row.last_message_sender === 'Renter' ? 'Guest: ' : 'You: '}
                    </span>
                    {row.last_message_excerpt}
                  </>
                ) : (
                  <span className="faint">No messages could be read.</span>
                )}
              </div>
            </Link>
          ))}
        </div>
      ) : null}
    </>
  )
}
