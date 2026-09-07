import { useState } from 'react'
import { getCleanupWorkload, recordManualResolution } from '../api/agentguard'
import type { PricingCleanupRecord } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { PageHeader } from '../components/Layout'
import { ErrorState, Loading } from '../components/States'
import { ApiError } from '../api/client'

/**
 * What cleanup owes, and the one action a person can take about it.
 *
 * **Nothing on this page touches PriceLabs.** There is no delete, no retry,
 * no force, no run-a-pass button, and no way to aim anything at a night. The
 * only writable action records that a person already did something
 * *elsewhere* — and it is labelled to make that unmistakable, because the
 * failure mode here is someone clicking a button they think removes an
 * override and being wrong about it.
 *
 * A `DELETE_STARTED` row is shown and stays shown. Surfacing it is the whole
 * point — automation will never resolve it — but it is deliberately not
 * manually resolvable either: the question there is what the *provider* did,
 * which no amount of human confidence settles.
 */

/** Rows a person may close by hand. Mirrors `MANUALLY_RESOLVABLE_STATES`. */
const RESOLVABLE = new Set(['NEEDS_REVIEW', 'UNKNOWN_CLEANUP_STATE'])

const STATE_TONE: Record<string, string> = {
  PENDING_WRITE: 'tone-neutral',
  ACTIVE: 'tone-ok',
  CLAIMED: 'tone-warn',
  DELETE_STARTED: 'tone-bad',
  NEEDS_REVIEW: 'tone-warn',
  UNKNOWN_CLEANUP_STATE: 'tone-bad',
}

/** Hours old before the queue is behind rather than merely quiet. */
const STALE_AFTER_HOURS = 36

function money(value?: number | null): string {
  return value === null || value === undefined ? '$—' : `$${Math.round(value)}`
}

function when(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function Row({
  record,
  onResolved,
}: {
  record: PricingCleanupRecord
  onResolved: () => void
}) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const resolvable = RESOLVABLE.has(record.state)

  async function submit() {
    setBusy(true)
    setError(null)

    try {
      await recordManualResolution(record.id, text)
      onResolved()
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : 'Could not record the resolution.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <span className={`badge ${STATE_TONE[record.state] ?? 'tone-neutral'}`}>
          {record.state.replace(/_/g, ' ')}
        </span>
        <strong>
          {record.listing_id} · {record.stay_date}
        </strong>
        <span>
          {money(record.old_price)} &rarr; {money(record.new_price)} {record.currency}
        </span>
      </div>

      {/* Everything an administrator needs to find this override in the
          PriceLabs UI, and nothing that could act on it. */}
      <dl className="detail-grid">
        <dt>AgentGuard marker</dt>
        <dd className="mono">{record.marker ?? '—'}</dd>

        <dt>Reason we sent</dt>
        <dd className="mono">{record.reason_sent ?? 'never recorded'}</dd>

        <dt>Provider confirmed at</dt>
        <dd>{record.provider_created_at ?? 'never confirmed'}</dd>

        <dt>Cleanup was due</dt>
        <dd>{when(record.cleanup_at)}</dd>

        <dt>Approval / run</dt>
        <dd className="mono">
          {record.approval_id ?? '—'} / {record.run_id ?? '—'}
        </dd>

        <dt>What automation concluded</dt>
        <dd>{record.resolution ?? '—'}</dd>
      </dl>

      {record.state === 'DELETE_STARTED' ? (
        <p className="callout tone-bad">
          A delete may already be in flight for this date. Treat it as hands off and
          investigate in PriceLabs. It cannot be resolved from here.
        </p>
      ) : null}

      {resolvable ? (
        open ? (
          <div className="manual-resolution">
            {/* The sentence that has to be read before anyone clicks. */}
            <p className="callout tone-warn">
              This records something you already verified or performed manually. It does
              not change PriceLabs.
            </p>

            <label>
              What did you do or verify?
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={3}
                placeholder="e.g. Removed the $196 override for 2026-09-08 in the PriceLabs UI and re-read to confirm it is gone."
              />
            </label>

            {error ? <p className="callout tone-bad">{error}</p> : null}

            <div className="actions">
              <button
                type="button"
                className="primary"
                disabled={busy || text.trim().length === 0}
                onClick={submit}
              >
                Record manual resolution
              </button>
              <button type="button" onClick={() => setOpen(false)} disabled={busy}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button type="button" onClick={() => setOpen(true)}>
            Record manual resolution
          </button>
        )
      ) : null}
    </div>
  )
}

export function CleanupPage() {
  const workload = useAsync(() => getCleanupWorkload(), [])

  const stale =
    workload.data?.oldest_overdue_hours !== null &&
    workload.data?.oldest_overdue_hours !== undefined &&
    workload.data.oldest_overdue_hours > STALE_AFTER_HOURS

  return (
    <div className="page">
      <PageHeader
        title="Cleanup obligations"
        subtitle="Temporary overrides AgentGuard owes a removal for"
      />

      {workload.error ? <ErrorState error={workload.error} /> : null}
      {workload.loading && !workload.data ? <Loading /> : null}

      {workload.data ? (
        <>
          <div className="stat-row">
            <div className="stat">
              <div className="stat-label">Active</div>
              <div className="stat-value">{workload.data.active}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Due now</div>
              <div className="stat-value">{workload.data.due_now}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Claimed</div>
              <div className="stat-value">{workload.data.claimed}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Delete started</div>
              <div className="stat-value tone-bad">{workload.data.delete_started}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Needs review</div>
              <div className="stat-value tone-warn">{workload.data.needs_review}</div>
            </div>
          </div>

          <p className={`callout ${stale ? 'tone-warn' : 'tone-neutral'}`}>
            {stale
              ? `Cleanup is behind: the oldest overdue obligation has waited ${Math.round(
                  workload.data.oldest_overdue_hours ?? 0,
                )} hours.`
              : 'Cleanup is current.'}{' '}
            This page never removes an override. Running a cleanup pass is a separate,
            deliberate step.
          </p>

          <h2>Needs a person</h2>

          {workload.data.needs_attention.length === 0 ? (
            <p className="empty">
              Nothing is waiting on a person. Obligations automation can settle do not
              appear here.
            </p>
          ) : (
            workload.data.needs_attention.map((record) => (
              <Row
                key={record.id}
                record={record}
                onResolved={() => workload.reload()}
              />
            ))
          )}
        </>
      ) : null}
    </div>
  )
}
