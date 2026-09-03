import { Link } from 'react-router-dom'
import { getInbox, refreshInbox } from '../api/agentguard'
import type { ConversationStatus, DraftStatus } from '../api/types'
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

/**
 * How often prepared replies are brought up to date while the Inbox is open.
 *
 * Slower than the status poll on purpose: this one can spend model calls, and
 * the fingerprint makes it cheap only when nothing has changed. The webhook is
 * what makes drafting *fast*; this is what makes it *certain* -- a webhook that
 * never arrived, or whose background task died with the process, gets picked up
 * here.
 */
const PREPARE_MS = 120_000

const DRAFT_LABEL: Record<DraftStatus, string> = {
  DRAFT_READY: 'Draft ready',
  EDITED: 'Draft edited',
  NO_REPLY_NEEDED: 'No reply needed',
  NEEDS_HUMAN_REVIEW: 'Needs human review',
  STALE: 'Draft stale',
  SENT: 'Sent',
  DISCARDED: 'Discarded',
}

const DRAFT_TONE: Record<DraftStatus, string> = {
  DRAFT_READY: 'tone-ok',
  EDITED: 'tone-ok',
  NO_REPLY_NEEDED: 'tone-neutral',
  NEEDS_HUMAN_REVIEW: 'tone-danger',
  STALE: 'tone-warn',
  SENT: 'tone-info',
  DISCARDED: 'tone-neutral',
}

export function DraftBadge({ status }: { status: DraftStatus }) {
  return (
    <span className={`badge ${DRAFT_TONE[status] ?? 'tone-neutral'}`}>
      <span className="badge-dot" aria-hidden="true" />
      {DRAFT_LABEL[status] ?? status}
    </span>
  )
}

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

  // Preparing replies is a separate, slower loop than reading statuses. A
  // failure here must never take down the list, so its error is deliberately
  // ignored -- the next tick tries again.
  useAsync(refreshInbox, [], {
    intervalMs: PREPARE_MS,
    pollWhile: () => true,
  })

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

      {/*
        A partial scan, stated plainly and without alarm. The provider stopped
        answering part way through discovery, so the rows below were all read
        live but the list may be short. Saying nothing would be worse than a
        neutral notice: an operator would read a missing conversation as one
        that does not exist.
      */}
      {inbox.data?.incomplete ? (
        <div className="state state-warn" role="status" style={{ marginBottom: 16 }}>
          The booking provider did not answer for part of this scan, so some
          conversations may be missing. Everything shown was read live. The next poll
          tries again.
        </div>
      ) : null}

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
                  {row.draft ? <DraftBadge status={row.draft.status} /> : null}
                  <strong>{row.property_name ?? 'Unmapped property'}</strong>
                  {row.source ? <span className="faint">{row.source}</span> : null}
                </div>
                <span className="faint mono" style={{ fontSize: 12 }}>
                  {row.last_message_at ?? '—'}
                </span>
              </div>

              {row.draft?.message &&
              (row.draft.status === 'DRAFT_READY' ||
                row.draft.status === 'EDITED' ||
                row.draft.status === 'NEEDS_HUMAN_REVIEW') ? (
                <div className="draft-preview">
                  <span className="faint">Prepared reply: </span>
                  {row.draft.message}
                </div>
              ) : null}

              <div className="conversation-excerpt">
                {row.last_message_excerpt ? (
                  <>
                    <span className="faint">
                      {row.last_message_sender === 'Renter' ? 'Guest: ' : 'You: '}
                    </span>
                    {row.last_message_excerpt}
                  </>
                ) : row.preview_unavailable ? (
                  <span className="faint">Preview unavailable</span>
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
