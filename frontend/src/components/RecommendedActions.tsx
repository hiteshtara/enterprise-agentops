import { useState } from 'react'
import {
  getPricingRecommendations,
  getPricingSession,
  resolveApproval,
  submitPricingAction,
} from '../api/agentguard'
import { PricingSessionControl } from './PricingSessionControl'
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
 *
 * **Review reads; it does not begin anything.** It used to call
 * `submitPricingAction`, which parked a real DANGEROUS approval on the server
 * -- so with pricing writes switched off an owner could only ever reach a red
 * "Failed. ENABLE_PRICING_WRITES is not enabled", having created a run, an
 * approval and two audit events that could never succeed. A shut safety switch
 * is not a failed pricing action, and looking at the evidence for a
 * recommendation is not requesting one. Review is now pure local expansion of
 * the payload `GET /vacancy/recommendations` already returned: it issues no
 * request, so it *cannot* create anything.
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
    [
      'Proposed price',
      rec.proposed_price === null ? 'back to dynamic' : money(rec.proposed_price),
    ],
    [
      'Change',
      rec.pct_change === null
        ? '—'
        : `${rec.pct_change > 0 ? '+' : ''}${rec.pct_change}%`,
    ],
    ['Market p25', money(rec.market_p25)],
    ['Market booked median', money(rec.market_booked_median)],
    [
      'Market occupancy',
      rec.market_occupancy === null ? '—' : `${rec.market_occupancy.toFixed(0)}%`,
    ],
    [
      'Listing occupancy',
      rec.listing_occupancy === null ? '—' : `${rec.listing_occupancy.toFixed(0)}%`,
    ],
    ['Demand', rec.demand ?? '—'],
    ['Pinned at', money(rec.pinned_price)],
    ['Hard floor', money(rec.hard_floor)],
    // Two floors, and the difference matters to whoever is deciding: the hard
    // floor is refused outright, the owner floor asks them.
    ['Owner floor tonight', money(rec.owner_floor)],
    ['Auto-raise ceiling', money(rec.auto_raise_ceiling)],
    ['Absolute ceiling', money(rec.absolute_ceiling)],
    ['Confidence', rec.confidence],
    // What this property actually converts at in this lead band -- evidence
    // about the property, never a target price.
    [
      'Historical lead-band ADR',
      rec.historical_lead_band_adr === null
        ? '—'
        : `${money(rec.historical_lead_band_adr)} (n=${rec.history_sample_count ?? 0})`,
    ],
    [
      'Gap to that history',
      rec.historical_reference_gap_dollars === null
        ? '—'
        : `${money(rec.historical_reference_gap_dollars)} / ${rec.historical_reference_gap_pct ?? 0}%`,
    ],
    [
      'Observed Booking.com commission',
      rec.observed_commission_rate === null
        ? 'not observed'
        : `${rec.observed_commission_rate}%`,
    ],
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
      {rec.owner_floor_basis ? (
        <p className="vac-note">Owner floor basis: {rec.owner_floor_basis}</p>
      ) : null}
      {rec.below_owner_floor ? (
        <p className="vac-note vac-flag">
          This proposal is below the owner floor. It stays above the hard floor, but it
          is an explicit vacancy decision rather than a routine change.
        </p>
      ) : null}
      {rec.market_signal_conflict ? (
        <p className="vac-note vac-flag">
          Market-signal conflict: this property&rsquo;s own realised history and the
          comp set disagree about the direction. Near-term history takes precedence
          here; both sides are shown above.
        </p>
      ) : null}
      {rec.booking_com_warning ? (
        <p className="vac-note vac-flag">{rec.booking_com_warning}</p>
      ) : null}
      <p className="vac-reason">{rec.reason}</p>
      {rec.blocked_reason ? <p className="vac-note">{rec.blocked_reason}</p> : null}
      {rec.notes.map((note) => (
        <p key={note} className="vac-note">
          {note}
        </p>
      ))}
    </div>
  )
}

/**
 * What is actually permitted right now, said accurately.
 *
 * The page used to say "pricing writes are disabled" whenever any gate was
 * shut, which became untrue the moment one action type was released: an owner
 * reading it would have believed a control was off while it was on. It now
 * reports each action separately, and never describes a permitted action as
 * automatic -- every one still stops for an individual approval.
 */
function PolicyBanner({
  writesEnabled,
  unblocked,
}: {
  writesEnabled: boolean
  unblocked: string[]
}) {
  if (!writesEnabled || unblocked.length === 0) {
    return (
      <div className="card demo-note" role="note">
        <strong>Pricing changes are turned off.</strong> Recommendations are shown for
        review only. Nothing here can change a price.
      </div>
    )
  }

  const canRemovePin = unblocked.includes('REMOVE_PIN')
  const priceMovesOff = !unblocked.includes('LOWER') && !unblocked.includes('RAISE')

  return (
    <div className="card demo-note" role="note">
      {canRemovePin ? (
        <>
          <strong>Returning a date to dynamic pricing is enabled</strong> — and still
          asks you every time. Nothing is applied until you approve that exact date.
        </>
      ) : (
        <strong>Some pricing actions are enabled, each requiring approval.</strong>
      )}
      {priceMovesOff ? (
        <>
          {' '}
          Raising and lowering prices remain switched off pending expiry verification.
        </>
      ) : null}
    </div>
  )
}

function ActionCard({
  rec,
  writesEnabled,
}: {
  rec: PricingRecommendation
  /**
   * Whether a write is possible for *this listing, right now*: the deployment
   * kill switch, the deployment allowlist and an active session covering it,
   * all agreeing. Anything less and the card stays review-only, because
   * offering an approval that provably cannot execute is the bug this whole
   * piece of work removed.
   */
  writesEnabled: boolean
}) {
  const [reviewing, setReviewing] = useState(false)
  const [approval, setApproval] = useState<ApprovalRequest | null>(null)
  const [outcome, setOutcome] = useState<PricingOutcome | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [rejected, setRejected] = useState(false)

  const isRemovePin = rec.action === 'REMOVE_PIN'

  const keepLabel = isRemovePin ? `Keep ${money(rec.current_price)}` : 'Reject'

  const applyLabel = isRemovePin ? 'Return to dynamic pricing' : 'Approve & Apply'

  /**
   * Expand the evidence. **Deliberately not async and deliberately not a
   * request.** Everything shown is already in the payload the page fetched, so
   * reviewing a recommendation cannot create a run, an approval, an audit
   * event, a cleanup row, or reach PriceLabs -- not because it is careful, but
   * because it calls nothing.
   */
  function onReview() {
    setError(null)
    setReviewing(true)
  }

  /**
   * The separate, deliberate step: park a real approval on the server.
   *
   * Only reachable when pricing writes are actually available. Requesting an
   * approval that provably cannot execute is what produced doomed approvals
   * and a red failure box for a switch that was simply off.
   */
  async function onRequestApproval() {
    setBusy(true)
    setError(null)

    try {
      const response = await submitPricingAction(rec)

      setApproval(response.approval_required ?? null)
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

      <dl className="vac-plain">
        <dt>Current</dt>
        <dd className="mono">
          {money(rec.current_price)}
          {isRemovePin ? ' fixed' : null}
        </dd>
        <dt>Recommendation</dt>
        <dd>{rec.plain_action ?? rec.action}</dd>
        <dt>Reason</dt>
        <dd>{rec.plain_reason ?? rec.reason}</dd>
      </dl>

      {rec.requires_human ? (
        <p className="vac-note">Always requires a human decision.</p>
      ) : null}

      {error ? (
        <div className="state state-error" role="alert">
          {error instanceof ApiError ? error.message : GENERIC_FAILURE}
        </div>
      ) : null}

      {outcome && outcome.refusal ? (
        // A refusal means the action never left AgentGuard: a switch was off,
        // the state had moved, or a guardrail said no. Nothing was attempted,
        // so this is not a failure and is not red.
        <div className="state state-warn" role="status">
          <strong>Not attempted.</strong> {outcome.message}
        </div>
      ) : outcome ? (
        <div className={`state ${outcomeTone(outcome.outcome)}`} role="status">
          {outcome.outcome === 'CONFIRMED_APPLIED' ? (
            <>
              <strong>Override stored.</strong> PriceLabs accepted and persisted{' '}
              {money(outcome.old_price)} → {money(outcome.new_price)}, verified by
              re-reading. The guest-facing rate changes on the next PriceLabs refresh —
              this is not confirmation that it has.
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
            {isRemovePin
              ? 'This releases the date back to PriceLabs dynamic pricing. No booking or guest rate is affected. The server re-reads first and refuses if anything moved.'
              : 'Approving applies this one change in PriceLabs. The server re-reads first and refuses if anything moved.'}
          </p>
          <div className="row" style={{ gap: 8 }}>
            <button type="button" disabled={busy} onClick={() => onDecide(true)}>
              {applyLabel}
            </button>
            <button type="button" disabled={busy} onClick={() => onDecide(false)}>
              {keepLabel}
            </button>
          </div>
        </div>
      ) : rec.blocked_reason ? (
        // Unchanged behaviour: a verification gate blocks this action outright,
        // so there is nothing to review towards. The gate's own words are kept
        // alongside the owner-facing sentence.
        <div className="state state-warn" role="note">
          Price changes are currently disabled pending expiry verification.
          <div className="vac-note">{rec.blocked_reason}</div>
        </div>
      ) : reviewing ? (
        <div className="vac-review">
          <Evidence rec={rec} />

          {isRemovePin ? (
            <p className="vac-note">
              Removing this override returns pricing control for {rec.stay_date} to
              PriceLabs dynamic pricing. Nothing is written in its place. It does not
              guarantee a higher price, more revenue, or a booking — PriceLabs may price
              the night above or below {money(rec.current_price)}.
            </p>
          ) : null}

          {writesEnabled ? (
            <div className="row" style={{ gap: 8 }}>
              <button type="button" disabled={busy} onClick={onRequestApproval}>
                {busy ? 'Preparing…' : 'Request approval'}
              </button>
              <button type="button" disabled={busy} onClick={() => setReviewing(false)}>
                Close
              </button>
            </div>
          ) : (
            <>
              <div className="state state-warn" role="note">
                <strong>LIVE PRICING IS OFF</strong>
                <div>Review only — nothing can be sent to PriceLabs.</div>
              </div>
              <div className="row" style={{ gap: 8 }}>
                <button type="button" disabled title="Live pricing is off">
                  Request approval
                </button>
                <span className="faint">
                  Start a live pricing session to request approval.
                </span>
                <button type="button" onClick={() => setReviewing(false)}>
                  Close
                </button>
              </div>
            </>
          )}
        </div>
      ) : (
        <div className="row" style={{ gap: 8 }}>
          <button type="button" onClick={onReview}>
            Review
          </button>
          {!writesEnabled ? (
            <span className="faint">Live pricing is off — review only.</span>
          ) : null}
        </div>
      )}
    </div>
  )
}

export function RecommendedActions({
  canAdminister = false,
}: {
  canAdminister?: boolean
}) {
  const { data, error, loading } = useAsync(getPricingRecommendations, [])
  const session = useAsync(getPricingSession, [], { intervalMs: 15_000 })

  if (loading) return <Loading label="Building pricing recommendations" />
  if (error) return <ErrorState error={error} />
  if (!data) return null

  const actionable = data.recommendations.filter((rec) => rec.actionable)

  /**
   * All three layers must agree before a card offers a write path. The
   * session alone is never enough -- it narrows the deployment controls and
   * cannot widen them, so the console must not imply otherwise.
   */
  const live = session.data
  const liveFor = (listingId: string) =>
    Boolean(
      data.writes_enabled &&
      live?.deployment_writes_enabled &&
      live.mode === 'LIVE' &&
      live.listing_ids.includes(listingId),
    )

  return (
    <section className="vac-section">
      <h2 className="card-title">Recommended actions</h2>
      <p className="page-subtitle">
        Computed in Python from PriceLabs market data for these exact dates. No model
        scores these, and none of them changes a price until you approve one
        individually.
      </p>

      <PricingSessionControl bands={data.bands} canAdminister={canAdminister} />

      <PolicyBanner
        writesEnabled={data.writes_enabled}
        unblocked={data.unblocked_actions}
      />

      {actionable.length ? (
        <div className="grid">
          {actionable.map((rec) => (
            <ActionCard
              key={rec.id}
              rec={rec}
              writesEnabled={liveFor(rec.listing_id)}
            />
          ))}
        </div>
      ) : (
        <Empty message="No pricing action is recommended today." />
      )}
    </section>
  )
}
