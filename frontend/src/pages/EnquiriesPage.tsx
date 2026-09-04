import { useState } from 'react'
import { generateEnquiryReply, listEnquiries } from '../api/agentguard'
import type { EnquiryReplyDraft, EnquirySummary } from '../api/types'
import { ApiError } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

/**
 * The enquiry helper: a separate screen from the Inbox, on purpose.
 *
 * The Inbox is the booked-guest pipeline -- it discovers conversations, keeps
 * prepared replies, and can send one after a human approves it. This page does
 * none of that. It lists open enquiries, and when the operator presses Generate
 * reply it asks the backend for text to read and copy.
 *
 * Three absences are the design:
 *
 *   * **No send control**, here or anywhere behind it. There is no endpoint to
 *     call, so there is no button to hide.
 *   * **No polling and no auto-refresh.** The provider is read when a person
 *     asks, and at no other time. Refresh is a button.
 *   * **No stored draft.** A generated draft lives in this component until the
 *     page is reloaded; the backend keeps no enquiry-reply row for it. The
 *     banner says exactly that and no more -- drafting runs through the
 *     ordinary agent loop, so the prompt is persisted as a Run like any other,
 *     and a banner claiming nothing is stored at all would be untrue.
 */
const GENERIC_FAILURE = 'The reply could not be generated. Try again.'

/**
 * How many of the open enquiries this page is showing.
 *
 * Shown always, not only when the list is truncated: "Showing 12 of 12" is the
 * sentence that makes "Showing 20 of 47" believable, and a count that appears
 * only sometimes is a count nobody trusts. No limit selector -- the number is
 * here to be honest about the queue, not to page through it.
 */
function countLine(shown: number, total: number): string {
  const enquiries = total === 1 ? 'open enquiry' : 'open enquiries'

  return shown < total
    ? `Showing ${shown} of ${total} ${enquiries}`
    : `Showing ${total} ${enquiries}`
}

function stayDates(enquiry: EnquirySummary): string {
  if (!enquiry.arrival && !enquiry.departure) return 'No dates requested'

  return `${enquiry.arrival ?? '—'} → ${enquiry.departure ?? '—'}`
}

function EnquiryRow({
  enquiry,
  draft,
  pending,
  onGenerate,
}: {
  enquiry: EnquirySummary
  draft: EnquiryReplyDraft | undefined
  pending: boolean
  onGenerate: () => void
}) {
  return (
    <article className="card" aria-label={enquiry.property_name ?? 'Enquiry'}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <div className="card-title">
            {enquiry.property_name ?? 'Unknown property'}
          </div>
          <div className="muted">
            {stayDates(enquiry)}
            {enquiry.source ? ` · ${enquiry.source}` : ''}
          </div>
        </div>

        <div className="row">
          <span className={`badge ${enquiry.is_replied ? 'tone-ok' : 'tone-warn'}`}>
            <span className="badge-dot" aria-hidden="true" />
            {enquiry.is_replied ? 'Replied' : 'Awaiting reply'}
          </span>

          <button type="button" onClick={onGenerate} disabled={pending}>
            {pending ? 'Generating…' : 'Generate reply'}
          </button>
        </div>
      </div>

      <div className="mono faint" style={{ marginTop: 6 }}>
        {enquiry.enquiry_ref}
      </div>

      {draft ? (
        <div className="stack" style={{ marginTop: 12 }}>
          {draft.message ? (
            <div className="answer">
              {draft.subject ? (
                <div className="guest-send-subject">{draft.subject}</div>
              ) : null}
              {/* Selectable text, never an editor: the operator copies this
                  into Lodgify themselves. */}
              <div className="message-body">{draft.message}</div>
            </div>
          ) : null}

          <p className="muted">{draft.detail}</p>
        </div>
      ) : null}
    </article>
  )
}

export function EnquiriesPage() {
  const { data, error, loading, reload } = useAsync(() => listEnquiries(), [])

  const [drafts, setDrafts] = useState<Record<string, EnquiryReplyDraft>>({})
  const [pending, setPending] = useState<string | null>(null)

  async function generate(enquiryRef: string) {
    setPending(enquiryRef)

    try {
      const draft = await generateEnquiryReply(enquiryRef)

      setDrafts((previous) => ({ ...previous, [enquiryRef]: draft }))
    } catch (failure) {
      // Only an ApiError's own message is ever shown; anything else is
      // reported generically.
      setDrafts((previous) => ({
        ...previous,
        [enquiryRef]: {
          enquiry_ref: enquiryRef,
          subject: null,
          message: null,
          detail: failure instanceof ApiError ? failure.message : GENERIC_FAILURE,
        },
      }))
    } finally {
      setPending(null)
    }
  }

  return (
    <>
      <PageHeader
        title="Enquiries"
        subtitle="Open enquiries, and a reply drafted on request."
        actions={
          <button type="button" onClick={() => reload()} disabled={loading}>
            Refresh
          </button>
        }
      />

      <div className="state-note" style={{ marginBottom: 14 }}>
        Generated text is a draft for you to review and copy into Lodgify. AgentGuard
        will not send it or save it as an enquiry reply.
      </div>

      {loading ? <Loading label="Loading enquiries" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data && data.enquiries.length === 0 ? (
        <Empty message="No open enquiries." />
      ) : null}

      {data && data.enquiries.length > 0 ? (
        <p className="muted" style={{ marginBottom: 10 }}>
          {countLine(data.enquiries.length, data.total)}
        </p>
      ) : null}

      {data && data.enquiries.length > 0 ? (
        <div className="stack">
          {data.enquiries.map((enquiry) => (
            <EnquiryRow
              key={enquiry.enquiry_ref}
              enquiry={enquiry}
              draft={drafts[enquiry.enquiry_ref]}
              pending={pending === enquiry.enquiry_ref}
              onGenerate={() => generate(enquiry.enquiry_ref)}
            />
          ))}
        </div>
      ) : null}
    </>
  )
}
