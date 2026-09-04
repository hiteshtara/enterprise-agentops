import { useState } from 'react'
import {
  getPricingRecommendations,
  resolveApproval,
  submitPricingAction,
} from '../api/agentguard'
import type {
  ApprovalRequest,
  PricingOutcome,
  PricingRecommendation,
} from '../api/types'
import { ApiError } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { Empty, ErrorState, Loading } from '../components/States'

/**
 * Recommended pricing actions, and the approval gate in front of them.
 *
 * Three rules this component exists to enforce visually:
 *
 *   * **No one-click price change.** Review is a separate step from approval,
 *     and the calendar carries no price control at all. A change is only ever
 *     approved from a card that is already showing every guardrail it sits
 *     inside.
 *   * **Nothing is applied from the browser.** "Approve & Apply" resolves a
 *     server-side approval; the write happens in Python, after the backend
 *     re-reads PriceLabs and refuses if the state moved.
 *   * **An unknown outcome is not a failure and offers no retry.** The price
 *     may already be live, so the card says to check PriceLabs and stops.
 */
const GENERIC_FAILURE = 'The action could not be submitted. Try again.'

function money(value: number | null | undefined): string {
  return value === null || value === undefined
    ? '—'
    : `$${Math.round(value).toLocaleString()}`
}

function tone(action: PricingRecommendation['action']): string {
  if (action === 'RAISE') return 'tone-ok'
  if (action === 'LOWER') return 'tone-warn'

  return 'tone-info'
}

function outcomeTone(outcome: PricingOutcome['outcome']): string {
  if (outcome === 'CONFIRMED_APPLIED') return 'state-ok'
  if (outcome === 'CONFIRMED_FAILED') return 'state-error'

  return 'state-warn'
}

function Evidence({ rec }: { rec: PricingRecommendation }) {
  const rows: [string, string][] = [
    ['Days out', String(rec.days_out)],
    ['Current price', money(rec.current_price)],
    ['Proposed price', rec.proposed_price === null ? 'back to dynamic' : money(rec.proposed_price)],
    ['Change', rec.pct_change === null ? '—' : `${rec.pct_change > 0 ? '+' : ''}${rec.pct_change}%`],
    ['Market p25', money(rec.market_p25)],
    ['Market booked median', money(rec.market_booked_median)],
    ['Market occupancy', rec.market_occupancy === null ? '—' : `${rec.market_occupancy.toFixed(0)}%`],
    ['Listing occupancy', rec.listing_occupancy === null ? '—' : `${rec.listing_occupancy.toFixed(0)}%`],
    ['Demand', rec.demand ?? '—'],
    ['Pinned at', money(rec.pinned_price)],
    ['Hard floor', money(rec.hard_floor)],
    ['Normal floor', money(rec.normal_floor)],
    ['Auto-raise ceiling', money(rec.auto_raise_ceiling)],
    ['Absolute ceiling', money(rec.absolute_ceiling)],
    ['Confidence', rec.confidence],
    ['PriceLabs refreshed', rec.last_refreshed_at ?? 'unknown'],
    ['State fingerprint', rec.fingerprint],
  ]

  return (
    <div className="vac-evidence">
      <dl>
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd className="mono">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="vac-reason">{rec.reason}</p>
      {rec.notes.map((note) => (
        <p key={note} className="vac-note">
          {note}
        </p>
      ))}
    </div>
  )
}

function ActionCard({
  rec,
  writesEnabled,
}: {
  rec: PricingRecommendation
  writesEnabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [approval, setApproval] = useState<ApprovalRequest | null>(null)
  const [outcome, setOutcome] = useState<PricingOutcome | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [rejected, setRejected] = useState(false)

  async function onReview() {
    setBusy(true)
    setError(null)

    try {
      const response = await submitPricingAction(rec)

      setApproval(response.approval_required ?? null)
      setOpen(true)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  async function onDecide(approved: boolean) {
    if (!approval) return

    setBusy(true)
    setError(null)

    try {
      const response = await resolveApproval(approval.approval_id, approved)

      if (!approved) {
        setRejected(true)
      } else {
        setOutcome((response.result ?? null) as PricingOutcome | null)
      }

      setApproval(null)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card vac-action">
      <div className="vac-head">
        <div>
          <strong>{rec.display_name}</strong>{' '}
          <span className="mono muted">{rec.stay_date}</span>
          <div className="faint">{rec.days_out} days out</div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <span className={`badge ${tone(rec.action)}`}>
            <span className="badge-dot" aria-hidden="true" />
            {rec.action.replace('_', ' ')}
          </span>
          <span className="badge tone-neutral">
            <span className="badge-dot" aria-hidden="true" />
            {rec.confidence}
          </span>
        </div>
      </div>

      <div className="vac-action-prices">
        <span className="mono">{money(rec.current_price)}</span>
        <span aria-hidden="true"> → </span>
        <span className="mono vac-figure">
          {rec.proposed_price === null ? 'dynamic' : money(rec.proposed_price)}
        </span>
        {rec.pct_change !== null ? (
          <span className="faint mono">
            {' '}
            ({rec.pct_change > 0 ? '+' : ''}
            {rec.pct_change}%)
          </span>
        ) : null}
      </div>

      {rec.requires_human ? (
        <p className="vac-note">Always requires a human decision.</p>
      ) : null}

      {open ? <Evidence rec={rec} /> : null}

      {error ? (
        <div className="state state-error" role="alert">
          {error instanceof ApiError ? error.message : GENERIC_FAILURE}
        </div>
      ) : null}

      {outcome ? (
        <div className={`state ${outcomeTone(outcome.outcome)}`} role="status">
          {outcome.outcome === 'CONFIRMED_APPLIED' ? (
            <>
              <strong>Override stored.</strong> PriceLabs accepted and persisted{' '}
              {money(outcome.old_price)} → {money(outcome.new_price)}, verified by
              re-reading. The guest-facing rate changes on the next PriceLabs
              refresh — this is not confirmation that it has.
            </>
          ) : outcome.outcome === 'CONFIRMED_FAILED' ? (
            <>Failed. {outcome.message}</>
          ) : (
            <>State unknown — check PriceLabs before doing anything else.</>
          )}
        </div>
      ) : rejected ? (
        <div className="state" role="status">
          Rejected. Nothing was changed.
        </div>
      ) : approval ? (
        <div className="vac-approve">
          <p className="faint">
            Approving applies this one change in PriceLabs. The server re-reads
            first and refuses if anything moved.
          </p>
          <div className="row" style={{ gap: 8 }}>
            <button type="button" disabled={busy} onClick={() => onDecide(true)}>
              Approve &amp; Apply
            </button>
            <button type="button" disabled={busy} onClick={() => onDecide(false)}>
              Reject
            </button>
          </div>
        </div>
      ) : rec.blocked_reason ? (
        <div className="state state-warn" role="note">
          <strong>Blocked pending live verification.</strong>{' '}
          {rec.blocked_reason}
        </div>
      ) : (
        <div className="row" style={{ gap: 8 }}>
          <button type="button" disabled={busy} onClick={onReview}>
            {busy ? 'Preparing…' : 'Review'}
          </button>
          {!writesEnabled ? (
            <span className="faint">
              Pricing writes are disabled — review only.
            </span>
          ) : null}
        </div>
      )}
    </div>
  )
}

export function RecommendedActions() {
  const { data, error, loading } = useAsync(getPricingRecommendations, [])

  if (loading) return <Loading label="Building pricing recommendations" />
  if (error) return <ErrorState error={error} />
  if (!data) return null

  const actionable = data.recommendations.filter((rec) => rec.actionable)

  return (
    <section className="vac-section">
      <h2 className="card-title">Recommended actions</h2>
      <p className="page-subtitle">
        Computed in Python from PriceLabs market data for these exact dates. No
        model scores these, and none of them changes a price until you approve
        one individually.
      </p>

      {!data.writes_enabled ? (
        <div className="card demo-note" role="note">
          <strong>Pricing writes are disabled.</strong> Recommendations are shown
          for review. Applying one requires <span className="mono">ENABLE_PRICING_WRITES</span>{' '}
          and that listing&rsquo;s own switch.
        </div>
      ) : null}

      {actionable.length ? (
        <div className="grid">
          {actionable.map((rec) => (
            <ActionCard key={rec.id} rec={rec} writesEnabled={data.writes_enabled} />
          ))}
        </div>
      ) : (
        <Empty message="No pricing action is recommended today." />
      )}
    </section>
  )
}
