import { getPricingOutcomes, getReconcilerHealth } from '../api/agentguard'
import type { PricingActionOutcome, ReconcilerHealth } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { useUrlFilter } from '../hooks/useUrlFilter'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

/**
 * What happened after each executed pricing action. **Read-only.**
 *
 * This page imports no write function and has no approve path. It answers
 * one question -- a price was changed, and then what was observed about that
 * night -- and deliberately refuses to answer the adjacent question everyone
 * will want it to: whether the change *caused* anything. We do not observe
 * the counterfactual, so there is no uplift figure, no attributed revenue and
 * no ROI here, and the disclaimer comes from the API payload rather than
 * being written locally, so every consumer states the same thing.
 *
 * Three display rules carry most of the honesty:
 *
 * - **Unknown is rendered as unknown.** A row not yet reconciled shows
 *   "Not yet reconciled", never "did not book" and never `$0.00`.
 * - **Hours, not days.** A booking 6.5 hours after a reduction is shown as
 *   6.5h. Rounding it to "0 days" would erase the most interesting signal
 *   this table can produce.
 * - **ADR is labelled a stay average.** The provider reports revenue per
 *   reservation, not per night, so comparing it to the night's price is
 *   wrong and the column header says so.
 */

/** The actions this board can filter to. Anything else in the URL is ignored. */
const ACTIONS = ['LOWER', 'RAISE', 'REMOVE_PIN'] as const

type Action = (typeof ACTIONS)[number]

const STATUS_TONE: Record<string, string> = {
  booked: 'tone-ok',
  cancelled: 'tone-warn',
  none: 'tone-neutral',
}

/** Hours old before a reconciler is treated as behind rather than merely idle. */
const STALE_AFTER_HOURS = 36

function money(value?: number | null): string {
  return value === null || value === undefined ? '$—' : `$${Math.round(value)}`
}

function count(value?: number | null): string {
  return value === null || value === undefined ? '—' : String(value)
}

function hours(value?: number | null): string {
  if (value === null || value === undefined) return '—'

  return value < 48 ? `${value.toFixed(1)}h` : `${(value / 24).toFixed(1)}d`
}

function when(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

/**
 * The outcome of one row, in words.
 *
 * The four cases are distinct on purpose. "Booked, then cancelled" is not the
 * same as "not booked", and a night that was rebooked keeps both facts.
 */
function outcomeLabel(row: PricingActionOutcome): string {
  if (row.current_booking_status === null || row.current_booking_status === undefined) {
    return 'Not yet reconciled'
  }

  if (row.current_booking_status === 'booked') {
    return row.cancelled_after_booking ? 'Booked again after a cancellation' : 'Booked'
  }

  if (row.cancelled_after_booking) return 'Booked after the action, later cancelled'

  return row.first_booked_at ? 'Cancelled' : 'Never booked'
}

function Freshness({ health }: { health: ReconcilerHealth | null }) {
  if (!health) return null

  const age = health.oldest_unreconciled_age_hours
  const stale = age !== null && age !== undefined && age > STALE_AFTER_HOURS

  return (
    <div className={`callout ${stale ? 'tone-warn' : 'tone-neutral'}`}>
      <strong>{stale ? 'Reconciliation is behind.' : 'Reconciliation'}</strong>{' '}
      {health.unreconciled} of {health.outcomes} actions not yet reconciled
      {age !== null && age !== undefined
        ? `; the oldest has waited ${hours(age)}`
        : ''}
      . Last successful pass: {when(health.last_successful_reconciliation_at)}.
      {stale
        ? ' Until it catches up, an unreconciled row means unknown — not that the night went unsold.'
        : ''}
    </div>
  )
}

export function OutcomesPage() {
  const [action, setAction] = useUrlFilter<Action>('action', ACTIONS)

  const outcomes = useAsync(() => getPricingOutcomes(action ? { action } : {}), [action])
  const health = useAsync(() => getReconcilerHealth(), [])

  return (
    <div className="page">
      <PageHeader
        title="Pricing outcomes"
        subtitle="What happened after each executed pricing action"
      />

      {outcomes.error ? <ErrorState error={outcomes.error} /> : null}
      {outcomes.loading && !outcomes.data ? <Loading /> : null}

      {outcomes.data ? (
        <>
          <Freshness health={health.data ?? null} />

          <div className="stat-row">
            <div className="stat">
              <div className="stat-label">Actions executed</div>
              <div className="stat-value">{outcomes.data.executed}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Booked afterwards</div>
              <div className="stat-value tone-ok">
                {outcomes.data.booked_after_action}
              </div>
            </div>
            <div className="stat">
              <div className="stat-label">Booked, then cancelled</div>
              <div className="stat-value tone-warn">
                {outcomes.data.booked_then_cancelled}
              </div>
            </div>
            <div className="stat">
              <div className="stat-label">Never booked</div>
              <div className="stat-value">{outcomes.data.never_booked}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Unknown</div>
              <div className="stat-value tone-neutral">{outcomes.data.unknown}</div>
            </div>
          </div>

          {/* Straight from the API payload, not written here, so the API and
              the console can never state different things about causation. */}
          <p className="disclaimer">{outcomes.data.disclaimer}</p>

          <div className="filter-row">
            <label>
              Action{' '}
              <select
                value={action}
                onChange={(e) => setAction(e.target.value as Action | '')}
              >
                <option value="">All</option>
                {ACTIONS.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {outcomes.data.outcomes.length === 0 ? (
            <Empty
              message={
                'No pricing actions have been executed yet. An empty board is a ' +
                'legitimate state, not a failure.'
              }
            />
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Property</th>
                    <th>Stay date</th>
                    <th>Action</th>
                    <th>Price</th>
                    <th>Days out</th>
                    <th>Evidence at decision</th>
                    <th>Outcome</th>
                    <th>First booked</th>
                    <th>After action</th>
                    <th>Lead</th>
                    <th>Channel</th>
                    <th title="rental_revenue / no_of_days, averaged across the whole stay">
                      Realized stay ADR — stay average, not this night&rsquo;s rate
                    </th>
                    <th>Reconciled</th>
                  </tr>
                </thead>
                <tbody>
                  {outcomes.data.outcomes.map((row) => (
                    <tr key={row.id}>
                      <td>{row.listing_id}</td>
                      <td>{row.stay_date}</td>
                      <td>{row.action}</td>
                      <td>
                        {money(row.price_before)} &rarr; {money(row.price_after)}
                      </td>
                      <td>{count(row.days_out)}</td>
                      <td className="evidence">
                        {row.history_adr
                          ? `history ${money(row.history_adr)} (n=${count(
                              row.history_sample_count,
                            )})`
                          : 'history —'}
                        {'; '}
                        {`p25 ${money(row.market_p25)}`}
                        {row.demand ? `; ${row.demand}` : ''}
                        {row.market_signal_conflict ? '; signal conflict' : ''}
                      </td>
                      <td>
                        <span
                          className={`badge ${
                            STATUS_TONE[row.current_booking_status ?? ''] ??
                            'tone-neutral'
                          }`}
                        >
                          {outcomeLabel(row)}
                        </span>
                      </td>
                      <td>{when(row.first_booked_at)}</td>
                      <td>{hours(row.first_hours_from_action)}</td>
                      <td>
                        {row.first_booking_lead_days === null ||
                        row.first_booking_lead_days === undefined
                          ? '—'
                          : `${row.first_booking_lead_days}d`}
                      </td>
                      <td>{row.first_booking_channel ?? '—'}</td>
                      <td>{money(row.first_realized_stay_adr)}</td>
                      <td>
                        {row.last_reconciled_at ? when(row.last_reconciled_at) : 'never'}
                        {row.finalized_at ? ' · polling stopped' : ''}
                        {row.reopened_at ? ' · reopened' : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="footnote">
            {'"Polling stopped" means routine reconciliation ended for that row, not '}
            {'that the record can never change. A later pass that disagrees reopens it. '}
            {'Cancellation times are when AgentGuard noticed — PriceLabs does not '}
            {'report when a cancellation happened.'}
          </p>
        </>
      ) : null}
    </div>
  )
}
