import { useState } from 'react'
import {
  generateEnquiryReply,
  listEnquiries,
  requestEnquiryReply,
  resolveApproval,
} from '../api/agentguard'
import type {
  ApprovalRequest,
  EnquiryReplyDraft,
  EnquirySendOutcome,
  EnquirySummary,
} from '../api/types'
import { ApiError } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { PageHeader } from '../components/Layout'
import { ApprovalCard } from '../components/ApprovalCard'
import { Empty, ErrorState, Loading } from '../components/States'

/**
 * The enquiry helper: a separate screen from the Inbox, on purpose.
 *
 * The Inbox is the booked-guest pipeline -- it discovers conversations, keeps
 * prepared replies, and sends one after a human approves it. This page shares
 * only the last of those. It lists open enquiries, generates a reply when the
 * operator presses the button, lets them edit it, and submits it for the same
 * approval gate every DANGEROUS action goes through.
 *
 * Three things stay true whatever this page does:
 *
 *   * **Nothing is sent from the browser.** "Send for approval" creates a
 *     parked run; the send happens server-side after a person approves, and
 *     the console has no path that reaches Lodgify directly.
 *   * **No polling and no auto-refresh.** The provider is read when a person
 *     asks, and at no other time. Refresh is a button.
 *   * **No stored draft.** A generated draft lives in this component until the
 *     page is reloaded; the backend keeps no enquiry-reply row for it.
 *
 * And one that is about what a person is *not* offered: after an uncertain
 * send there is no retry control, because the message may already have
 * arrived. See `LOCKED`.
 */
const GENERIC_FAILURE = 'The reply could not be generated. Try again.'

const DEFAULT_SUBJECT = 'Re: your enquiry'

/**
 * Outcomes after which this row offers no way to send again.
 *
 * `confirmed_sent` is obvious. `unknown_send_state` is the one that matters:
 * the message may already have reached a real person, so resending it is not a
 * retry of a failed action -- it is a second message. The row locks, says so,
 * and asks for a person to check the thread. `confirmed_failed` is not here:
 * nothing was sent, so composing again is not a duplicate.
 */
const LOCKED: EnquirySendOutcome['status'][] = ['confirmed_sent', 'unknown_send_state']

const UNKNOWN_REVIEW =
  'Needs a person: open the thread in Lodgify and check whether this message arrived before doing anything else.'

const FAILED_REVIEW =
  'Nothing was sent. Review the reply and submit it again if it is still right.'

function outcomeTone(status: EnquirySendOutcome['status']): string {
  if (status === 'confirmed_sent') return 'state-ok'
  if (status === 'confirmed_failed') return 'state-error'

  return 'state-warn'
}

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
  approval,
  outcome,
  error,
  busy,
  onGenerate,
  onSubmit,
  onDecide,
}: {
  enquiry: EnquirySummary
  draft: EnquiryReplyDraft | undefined
  approval: ApprovalRequest | undefined
  outcome: EnquirySendOutcome | undefined
  error: unknown
  busy: 'generate' | 'submit' | 'decide' | null
  onGenerate: () => void
  onSubmit: (subject: string, message: string) => void
  onDecide: (approved: boolean) => Promise<void>
}) {
  const [subject, setSubject] = useState(DEFAULT_SUBJECT)
  const [message, setMessage] = useState('')

  // Which draft the editor currently shows, tracked by the draft object's own
  // identity: a fresh Generate returns a new object and reseeds the boxes,
  // while a re-render must never overwrite what the operator is typing.
  //
  // Adjusted during render rather than in an effect, so the generated text is
  // present in the very commit that first shows it -- there is no frame in
  // which the box is empty and a keystroke could land in it.
  const [seeded, setSeeded] = useState<EnquiryReplyDraft | null>(null)

  if (draft && draft !== seeded) {
    setSeeded(draft)
    setSubject(draft.subject ?? DEFAULT_SUBJECT)
    setMessage(draft.message ?? '')
  }

  const locked = outcome !== undefined && LOCKED.includes(outcome.status)
  const composing = draft !== undefined && approval === undefined && !locked

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
          <span
            className={`badge ${
              outcome?.status === 'confirmed_sent' || enquiry.is_replied
                ? 'tone-ok'
                : 'tone-warn'
            }`}
          >
            <span className="badge-dot" aria-hidden="true" />
            {outcome?.status === 'confirmed_sent'
              ? 'Sent'
              : enquiry.is_replied
                ? 'Replied'
                : 'Awaiting reply'}
          </span>

          <button
            type="button"
            onClick={onGenerate}
            disabled={busy !== null || locked || approval !== undefined}
          >
            {busy === 'generate' ? 'Generating…' : 'Generate reply'}
          </button>
        </div>
      </div>

      <div className="mono faint" style={{ marginTop: 6 }}>
        {enquiry.enquiry_ref}
      </div>

      {outcome ? (
        <div
          className={`state ${outcomeTone(outcome.status)}`}
          role="status"
          style={{ marginTop: 12 }}
        >
          <div>{outcome.message}</div>
          {outcome.status === 'unknown_send_state' ? <div>{UNKNOWN_REVIEW}</div> : null}
          {outcome.status === 'confirmed_failed' ? <div>{FAILED_REVIEW}</div> : null}
        </div>
      ) : null}

      {error ? <ErrorState error={error} /> : null}

      {approval ? (
        <div style={{ marginTop: 12 }}>
          <ApprovalCard
            tool={approval.tool}
            risk={approval.risk}
            args={approval.arguments}
            runId={approval.run_id}
            busy={busy === 'decide'}
            onDecision={onDecide}
            requestedBy={approval.requested_by_user_id}
            context={{ property: enquiry.property_name, source: enquiry.source }}
          />
        </div>
      ) : null}

      {draft ? (
        <p className="muted" style={{ marginTop: 12 }}>
          {draft.detail}
        </p>
      ) : null}

      {composing ? (
        <div className="stack" style={{ marginTop: 8 }}>
          <div>
            <label className="field-label" htmlFor={`subject-${enquiry.enquiry_ref}`}>
              Subject
            </label>
            <input
              id={`subject-${enquiry.enquiry_ref}`}
              value={subject}
              onChange={(event) => setSubject(event.target.value)}
              disabled={busy !== null}
            />
          </div>

          <div>
            <label className="field-label" htmlFor={`message-${enquiry.enquiry_ref}`}>
              Message
            </label>
            <textarea
              id={`message-${enquiry.enquiry_ref}`}
              value={message}
              placeholder="Edit the draft, or write the reply yourself."
              onChange={(event) => setMessage(event.target.value)}
              disabled={busy !== null}
            />
          </div>

          <div className="row" style={{ justifyContent: 'space-between' }}>
            <span className="faint" style={{ fontSize: 12 }}>
              Nothing is sent until a human approves it.
            </span>
            <button
              type="button"
              className="primary"
              onClick={() => onSubmit(subject, message)}
              disabled={busy !== null || !subject.trim() || !message.trim()}
            >
              {busy === 'submit' ? 'Submitting…' : 'Send for approval'}
            </button>
          </div>
        </div>
      ) : null}
    </article>
  )
}

export function EnquiriesPage() {
  const { data, error, loading, reload } = useAsync(() => listEnquiries(), [])

  const [drafts, setDrafts] = useState<Record<string, EnquiryReplyDraft>>({})
  const [approvals, setApprovals] = useState<Record<string, ApprovalRequest>>({})
  const [outcomes, setOutcomes] = useState<Record<string, EnquirySendOutcome>>({})
  const [errors, setErrors] = useState<Record<string, unknown>>({})
  const [busy, setBusy] = useState<{
    ref: string
    kind: 'generate' | 'submit' | 'decide'
  } | null>(null)

  function record<T>(
    setter: React.Dispatch<React.SetStateAction<Record<string, T>>>,
    enquiryRef: string,
    value: T,
  ) {
    setter((previous) => ({ ...previous, [enquiryRef]: value }))
  }

  function forget<T>(
    setter: React.Dispatch<React.SetStateAction<Record<string, T>>>,
    enquiryRef: string,
  ) {
    setter((previous) => {
      const { [enquiryRef]: _removed, ...rest } = previous

      return rest
    })
  }

  async function generate(enquiryRef: string) {
    setBusy({ ref: enquiryRef, kind: 'generate' })
    forget(setErrors, enquiryRef)
    forget(setOutcomes, enquiryRef)

    try {
      record(setDrafts, enquiryRef, await generateEnquiryReply(enquiryRef))
    } catch (failure) {
      // Only an ApiError's own message is ever shown; anything else is
      // reported generically.
      record<EnquiryReplyDraft>(setDrafts, enquiryRef, {
        enquiry_ref: enquiryRef,
        subject: null,
        message: null,
        detail: failure instanceof ApiError ? failure.message : GENERIC_FAILURE,
      })
    } finally {
      setBusy(null)
    }
  }

  async function submit(enquiryRef: string, subject: string, message: string) {
    if (!subject.trim() || !message.trim()) return

    setBusy({ ref: enquiryRef, kind: 'submit' })
    forget(setErrors, enquiryRef)

    try {
      const response = await requestEnquiryReply(enquiryRef, subject, message)

      if (response.approval_required) {
        record(setApprovals, enquiryRef, response.approval_required)
      }
    } catch (failure) {
      record(setErrors, enquiryRef, failure)
    } finally {
      setBusy(null)
    }
  }

  async function decide(enquiryRef: string, approved: boolean) {
    const approval = approvals[enquiryRef]

    if (!approval) return

    setBusy({ ref: enquiryRef, kind: 'decide' })
    forget(setErrors, enquiryRef)

    try {
      const response = await resolveApproval(approval.approval_id, approved)

      forget(setApprovals, enquiryRef)

      if (approved && response.result) {
        record(setOutcomes, enquiryRef, response.result as EnquirySendOutcome)
      }
    } catch (failure) {
      record(setErrors, enquiryRef, failure)
    } finally {
      setBusy(null)
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
        Generated text is a draft for you to review and edit. Nothing reaches the person
        who enquired until you send it for approval and a human approves it.
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
              approval={approvals[enquiry.enquiry_ref]}
              outcome={outcomes[enquiry.enquiry_ref]}
              error={errors[enquiry.enquiry_ref]}
              busy={busy?.ref === enquiry.enquiry_ref ? busy.kind : null}
              onGenerate={() => generate(enquiry.enquiry_ref)}
              onSubmit={(subject, message) =>
                submit(enquiry.enquiry_ref, subject, message)
              }
              onDecide={(approved) => decide(enquiry.enquiry_ref, approved)}
            />
          ))}
        </div>
      ) : null}
    </>
  )
}
