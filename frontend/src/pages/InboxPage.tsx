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
 *
 * Three minutes rather than thirty seconds because one load used to be
 * expensive: the booking list carries no last-message time, so ordering the
 * Inbox meant reading every thread -- about 155 provider requests per load
 * against this account, which was caught live returning 429 on the first
 * booking page and failed the whole request.
 *
 * Ordering now comes from the persisted activity index and only the threads on
 * the page are read, so a load is roughly the booking scan plus the page size.
 * This interval is left where it is deliberately: it is no longer holding back
 * a load problem, and lowering it is a separate decision to make against a live
 * measurement rather than a side effect of this change. Manual Refresh and the
 * webhook fast path are unchanged, so noticing a message quickly does not
 * depend on this number.
 */
const POLL_MS = 180_000

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

/**
 * The provider's conversation status, shown only while it still means
 * something to an operator.
 *
 * `needs_attention` says one thing -- the guest spoke last -- which is not the
 * same as "somebody has to do something". A guest's closing acknowledgement
 * leaves a thread `needs_attention` for good, and once AgentGuard has read that
 * exact state and recorded that no reply is needed, this badge was appearing
 * next to "No reply needed" on the same row and contradicting it.
 *
 * The server decides that (`operator_attention`, one derivation shared by the
 * Inbox and the conversation page). All this does is stop asserting a guest is
 * waiting when it has been told nobody is -- and only for that one label, since
 * "Responded" and "Unknown" contradict nothing. `operatorAttention` defaults to
 * true so a payload without the field renders exactly as it did before.
 */
export function StatusBadge({
  status,
  operatorAttention = true,
}: {
  status: ConversationStatus
  operatorAttention?: boolean
}) {
  if (status === 'needs_attention' && !operatorAttention) {
    return null
  }

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
        A short list, stated plainly and without alarm. Either the provider
        stopped answering part way through discovery, or a conversation exists
        that the activity index has not read yet -- both mean the same thing to
        an operator, so they get one line rather than two vocabularies. The rows
        below were all read live. Saying nothing would be worse than a neutral
        notice: an operator would read a missing conversation as one that does
        not exist.
      */}
      {inbox.data?.incomplete ? (
        <div className="state state-warn" role="status" style={{ marginBottom: 16 }}>
          Some conversations may be missing while activity is still being indexed, or
          because the booking provider did not answer for part of this scan. Everything
          shown was read live. The next poll picks up more.
        </div>
      ) : null}

      {/*
        Ordering is as fresh as the activity index, and the index is brought up
        to date in bounded batches rather than by re-reading every conversation
        on every poll. When the rows behind this page have not been re-checked
        recently, say so. Deliberately neutral rather than a warning: nothing is
        wrong, no row is hidden, and the only claim being withdrawn is that this
        order is exactly current.
      */}
      {inbox.data?.activity_stale ? (
        <div className="state state-note" role="status" style={{ marginBottom: 16 }}>
          This ordering may be behind. Conversation activity is re-checked in batches,
          so a conversation that moved very recently can take a few minutes to reach its
          place. Nothing is hidden.
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
                  <StatusBadge
                    status={row.status}
                    operatorAttention={row.operator_attention}
                  />
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
