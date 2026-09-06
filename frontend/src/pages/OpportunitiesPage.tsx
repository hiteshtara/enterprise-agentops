import { getRevenueOpportunities } from '../api/agentguard'
import type { LowerOpportunity, Priority, RevenueOpportunity } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { useUrlFilter } from '../hooks/useUrlFilter'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

/**
 * The 60-day revenue-opportunity board. **Read-only, by construction.**
 *
 * This page imports no write function. There is no Review button, no submit,
 * no approval: changing a price still goes through the Recommended actions
 * card on /vacancy and an individual approval. What lives here is the
 * question "where is money being left on the table", answered from the same
 * engine, so the two views can never disagree about what is actionable.
 *
 * An opportunity is a RAISE that already cleared every deterministic
 * guardrail, is not blocked by a verification gate, and rests on evidence
 * fresh enough to act on. HOLD is the engine saying no and never appears
 * here; LOWER stays blocked by the Booking.com exposure gate and is not
 * presented as something to act on.
 *
 * Uplift is `proposed - current` for one night. It is **not** expected
 * revenue -- it assumes every night books at the higher price, which is the
 * assumption this whole feature exists to question -- and the wording on the
 * page says so rather than leaving a reader to infer it.
 */
const SORTS = ['priority', 'uplift', 'date', 'property', 'confidence', 'days'] as const

type Sort = (typeof SORTS)[number]

const SORT_LABELS: Record<Sort, string> = {
  priority: 'Priority',
  uplift: 'Uplift',
  date: 'Date',
  property: 'Property',
  confidence: 'Confidence',
  days: 'Days out',
}

const PRIORITIES = ['REVIEW_NOW', 'WATCH', 'LOW_PRIORITY'] as const

const PRIORITY_LABELS: Record<Priority, string> = {
  REVIEW_NOW: 'Review now',
  WATCH: 'Watch',
  LOW_PRIORITY: 'Low priority',
}

/** Band order for the default sort. Mirrors `PRIORITY_ORDER` in Python. */
const PRIORITY_RANK: Record<Priority, number> = {
  REVIEW_NOW: 0,
  WATCH: 1,
  LOW_PRIORITY: 2,
}

const PRIORITY_TONE: Record<Priority, string> = {
  REVIEW_NOW: 'tone-ok',
  WATCH: 'tone-warn',
  LOW_PRIORITY: 'tone-neutral',
}

const CONFIDENCES = ['HIGH', 'MEDIUM'] as const

type ConfidenceFilter = (typeof CONFIDENCES)[number]

const WINDOWS = ['0-7', '8-14', '15-30', '31-60'] as const

type WindowFilter = (typeof WINDOWS)[number]

const WINDOW_BOUNDS: Record<WindowFilter, [number, number]> = {
  '0-7': [0, 7],
  '8-14': [8, 14],
  '15-30': [15, 30],
  '31-60': [31, 60],
}

function money(value: number | null | undefined): string {
  return value === null || value === undefined
    ? '—'
    : `$${Math.round(value).toLocaleString()}`
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(0)}%`
}

/** Confidence order for sorting: the most trusted first. */
const CONFIDENCE_RANK: Record<string, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 }

function compare(a: RevenueOpportunity, b: RevenueOpportunity, sort: Sort): number {
  if (sort === 'date') return a.stay_date.localeCompare(b.stay_date)
  if (sort === 'days') return a.days_out - b.days_out
  if (sort === 'property') {
    return (
      a.display_name.localeCompare(b.display_name) ||
      a.stay_date.localeCompare(b.stay_date)
    )
  }
  if (sort === 'confidence') {
    return (
      (CONFIDENCE_RANK[a.confidence] ?? 9) - (CONFIDENCE_RANK[b.confidence] ?? 9) ||
      b.uplift - a.uplift
    )
  }

  if (sort === 'uplift') {
    return b.uplift - a.uplift || a.stay_date.localeCompare(b.stay_date)
  }

  // Default: the triage band, then the biggest number inside it. Mirrors the
  // order the server already returned, so the page opens on the same board
  // whether or not JavaScript re-sorts it.
  return (
    PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] ||
    b.uplift - a.uplift ||
    a.stay_date.localeCompare(b.stay_date)
  )
}

function Row({ row }: { row: RevenueOpportunity }) {
  return (
    <tr>
      <td>
        <span className={`badge ${PRIORITY_TONE[row.priority]}`}>
          <span className="badge-dot" aria-hidden="true" />
          {PRIORITY_LABELS[row.priority]}
        </span>
        {row.is_change_clamped ? (
          <div className="faint" title="The engine would have gone further.">
            capped at 10%
          </div>
        ) : null}
      </td>
      <td>
        <strong>{row.display_name}</strong>
        <div className="faint mono">{row.stay_date}</div>
      </td>
      <td className="mono">{row.days_out}d</td>
      <td className="mono">{money(row.current_price)}</td>
      <td className="mono">{money(row.proposed_price)}</td>
      <td className="mono">
        <strong>+{money(row.uplift)}</strong>
        <div className="faint">+{row.uplift_pct.toFixed(1)}%</div>
      </td>
      <td>
        <span className="badge tone-neutral">
          <span className="badge-dot" aria-hidden="true" />
          {row.confidence}
        </span>
      </td>
      <td className="mono">
        {money(row.market_p25)}
        <div className="faint">med {money(row.market_booked_median)}</div>
      </td>
      <td className="mono">
        {percent(row.market_occupancy)}
        <div className="faint">unit {percent(row.listing_occupancy)}</div>
      </td>
      <td>
        {row.demand ?? '—'}
        {row.events ? <div className="faint">{row.events}</div> : null}
      </td>
      <td className="mono">
        {money(row.owner_floor)}
        {row.owner_floor_basis ? (
          <div className="faint">{row.owner_floor_basis}</div>
        ) : null}
      </td>
      <td className="mono">{money(row.auto_raise_ceiling)}</td>
      <td className="mono">
        {row.pinned_price === null ? 'none' : `fixed ${money(row.pinned_price)}`}
      </td>
      <td className="faint">
        {row.why_now}
        <div className="faint">{row.reason}</div>
      </td>
    </tr>
  )
}

/**
 * Vacancy-fill reductions, kept visually and structurally apart from RAISE.
 *
 * They answer a different question -- "which empty nights am I about to lose"
 * rather than "where is money being left on the table" -- rank on proximity to
 * arrival rather than dollars, and rest on different evidence. Summing or
 * interleaving them would invite reading a reduction as a loss.
 *
 * Read-only, like the rest of this page. Applying a reduction happens under
 * Recommended actions on the Vacancy page, one date at a time, with an
 * approval that shows the same Booking.com warning.
 */
function LowerRow({ row }: { row: LowerOpportunity }) {
  const reduction =
    row.current_price !== null && row.proposed_price !== null
      ? row.current_price - row.proposed_price
      : null

  const reductionPct =
    reduction !== null && row.current_price
      ? (reduction / row.current_price) * 100
      : null

  return (
    <tr>
      <td>
        <span className={`badge ${PRIORITY_TONE[row.priority]}`}>
          <span className="badge-dot" aria-hidden="true" />
          {PRIORITY_LABELS[row.priority]}
        </span>
        {row.below_owner_floor ? (
          <div className="faint tone-warn">Below owner floor</div>
        ) : null}
        {row.market_signal_conflict ? (
          <div className="faint">Signal conflict</div>
        ) : null}
      </td>
      <td>
        <strong>{row.display_name}</strong>
        <div className="faint mono">{row.stay_date}</div>
      </td>
      <td className="mono">{row.days_out}d</td>
      <td className="mono">{money(row.current_price)}</td>
      <td className="mono">{money(row.proposed_price)}</td>
      <td className="mono">
        <strong>−{money(reduction)}</strong>
        <div className="faint">
          {reductionPct === null ? '—' : `−${reductionPct.toFixed(1)}%`}
        </div>
      </td>
      <td className="mono">
        {money(row.historical_lead_band_adr)}
        <div className="faint">n={row.history_sample_count}</div>
      </td>
      <td className="mono">
        {money(row.historical_reference_gap_dollars)}
        <div className="faint">
          {row.historical_reference_gap_pct === null
            ? '—'
            : `${row.historical_reference_gap_pct.toFixed(0)}%`}
        </div>
      </td>
      <td className="mono">
        {money(row.hard_floor)}
        <div className="faint">owner {money(row.owner_floor ?? row.normal_floor)}</div>
      </td>
      <td className="mono">
        {money(row.market_p25)}
        <div className="faint">{percent(row.market_occupancy)} mkt</div>
      </td>
      <td>
        {row.demand ?? '—'}
        <div className="faint">unit {percent(row.listing_occupancy)}</div>
      </td>
      <td className="mono">
        {row.observed_commission_rate === null
          ? 'not observed'
          : `${row.observed_commission_rate.toFixed(0)}%`}
      </td>
      <td className="faint">
        {row.why_now}
        {row.market_signal_conflict ? (
          <div className="vac-note">
            <strong>Market raise evidence:</strong> comparable listings at{' '}
            {money(row.market_p25)} p25.{' '}
            <strong>Property-history lower evidence:</strong>{' '}
            {money(row.historical_lead_band_adr)} realized at this lead time (n=
            {row.history_sample_count}). Because this night is close to arrival and
            demand is weak, the property&rsquo;s realized lead-time history takes
            precedence.
          </div>
        ) : null}
      </td>
    </tr>
  )
}

export function OpportunitiesPage() {
  const { data, error, loading } = useAsync(getRevenueOpportunities, [])

  const [sort, setSort] = useUrlFilter<Sort>('sort', SORTS)
  const [priority, setPriority] = useUrlFilter<Priority>('priority', PRIORITIES)
  const [property, setProperty] = useUrlFilter('property')
  const [confidence, setConfidence] = useUrlFilter<ConfidenceFilter>(
    'confidence',
    CONFIDENCES,
  )
  const [window, setWindow] = useUrlFilter<WindowFilter>('window', WINDOWS)

  if (loading) return <Loading label="Scanning the next 60 days" />
  if (error) return <ErrorState error={error} />
  if (!data) return null

  const properties = [
    ...new Map(data.opportunities.map((row) => [row.listing_id, row.display_name])),
  ].sort((a, b) => a[1].localeCompare(b[1]))

  const shown = data.opportunities
    .filter((row) => !priority || row.priority === priority)
    .filter((row) => !property || row.listing_id === property)
    .filter((row) => !confidence || row.confidence === confidence)
    .filter((row) => {
      if (!window) return true

      const [low, high] = WINDOW_BOUNDS[window]

      return row.days_out >= low && row.days_out <= high
    })
    .sort((a, b) => compare(a, b, (sort || 'priority') as Sort))

  // Recomputed from the rows on screen, so the headline always reconciles with
  // the column beneath it even when a filter is on.
  const shownUplift = shown.reduce((total, row) => total + row.uplift, 0)

  const filtered = shown.length !== data.opportunities.length

  return (
    <>
      <PageHeader
        title="Revenue opportunities"
        subtitle={`Open nights in the next ${data.horizon_days} days where the pricing engine would raise the price. Read-only: nothing here changes a price.`}
      />

      <div className="grid-stats">
        <div className="card">
          <div className="stat-label">Review now</div>
          <div className="stat-value tone-ok">{data.summary.review_now}</div>
        </div>
        <div className="card">
          <div className="stat-label">Watch</div>
          <div className="stat-value tone-warn">{data.summary.watch}</div>
        </div>
        <div className="card">
          <div className="stat-label">Low priority</div>
          <div className="stat-value">{data.summary.low_priority}</div>
        </div>
        <div className="card">
          <div className="stat-label">Opportunities</div>
          <div className="stat-value">{data.summary.opportunities}</div>
        </div>
        <div className="card">
          <div className="stat-label">Total nightly uplift</div>
          <div className="stat-value">{money(data.summary.total_uplift)}</div>
          <div className="faint">
            Sum of per-night price differences. Not a revenue forecast — it assumes
            every night books at the higher price.
          </div>
        </div>
        <div className="card">
          <div className="stat-label">High confidence</div>
          <div className="stat-value">{data.summary.high_confidence}</div>
        </div>
        <div className="card">
          <div className="stat-label">Medium confidence</div>
          <div className="stat-value">{data.summary.medium_confidence}</div>
        </div>
        <div className="card">
          <div className="stat-label">Properties</div>
          <div className="stat-value">{data.summary.properties}</div>
        </div>
      </div>

      <div className="card demo-note" role="note">
        <strong>Decision support only.</strong> Nothing on this page can change a price.
        Applying one still happens under Recommended actions on the Vacancy page, one
        date at a time, with an approval.
      </div>

      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <label>
          Priority{' '}
          <select
            value={priority}
            onChange={(e) => setPriority(e.target.value as Priority | '')}
          >
            <option value="">All</option>
            {PRIORITIES.map((value) => (
              <option key={value} value={value}>
                {PRIORITY_LABELS[value]}
              </option>
            ))}
          </select>
        </label>

        <label>
          Property{' '}
          <select value={property} onChange={(e) => setProperty(e.target.value)}>
            <option value="">All</option>
            {properties.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label>
          Confidence{' '}
          <select
            value={confidence}
            onChange={(e) => setConfidence(e.target.value as ConfidenceFilter | '')}
          >
            <option value="">All</option>
            {CONFIDENCES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>

        <label>
          Days out{' '}
          <select
            value={window}
            onChange={(e) => setWindow(e.target.value as WindowFilter | '')}
          >
            <option value="">All</option>
            {WINDOWS.map((value) => (
              <option key={value} value={value}>
                {value} days
              </option>
            ))}
          </select>
        </label>

        <label>
          Sort by{' '}
          <select
            value={sort || 'priority'}
            onChange={(e) => setSort(e.target.value as Sort)}
          >
            {SORTS.map((value) => (
              <option key={value} value={value}>
                {SORT_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {filtered ? (
        <p className="faint">
          Showing {shown.length} of {data.opportunities.length} — {money(shownUplift)}{' '}
          of {money(data.summary.total_uplift)} nightly uplift.
        </p>
      ) : null}

      <h2 className="card-title">Raise opportunities</h2>

      {shown.length ? (
        <div className="card" style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Priority</th>
                <th>Property / date</th>
                <th>Days out</th>
                <th>Current</th>
                <th>Proposed</th>
                <th>Uplift</th>
                <th>Confidence</th>
                <th>Market p25</th>
                <th>Occupancy</th>
                <th>Demand</th>
                <th>Owner floor</th>
                <th>Auto-raise ceiling</th>
                <th>Override</th>
                <th>Why now</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <Row key={row.id} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          message={
            data.opportunities.length
              ? 'No opportunity matches these filters.'
              : 'No raise is recommended in the next 60 days.'
          }
        />
      )}

      <h2 className="card-title">Lower / vacancy-fill opportunities</h2>

      {data.lower_summary ? (
        <div className="grid-stats">
          <div className="card">
            <div className="stat-label">Reductions</div>
            <div className="stat-value">{data.lower_summary.opportunities}</div>
          </div>
          <div className="card">
            <div className="stat-label">Review now</div>
            <div className="stat-value tone-ok">{data.lower_summary.review_now}</div>
          </div>
          <div className="card">
            <div className="stat-label">Watch</div>
            <div className="stat-value tone-warn">{data.lower_summary.watch}</div>
          </div>
          <div className="card">
            <div className="stat-label">Below owner floor</div>
            <div className="stat-value">{data.lower_summary.below_owner_floor}</div>
          </div>
          <div className="card">
            <div className="stat-label">Signal conflicts</div>
            <div className="stat-value">
              {data.lower_summary.market_signal_conflict}
            </div>
          </div>
        </div>
      ) : null}

      {/*
        Unconditional, above the table, never behind a fold. The exposure is
        unmeasured whether or not the owner has authorised acting on it, so
        this is not conditional on any flag -- and it is the one thing a
        reviewer must read before deciding to lower a published price.
      */}
      {data.lower_opportunities.length ? (
        <div className="card state-warn" role="note">
          <strong>Booking.com exposure is not fully verified.</strong> Actual
          reservations have shown stacked promotional/Genius discounts, and the maximum
          effective guest discount is unknown. Lowering the PriceLabs published price
          may therefore result in an even lower guest-facing rate.
          <div className="faint">
            Commission percentages shown below are{' '}
            <strong>observed on an actual August 2026 invoice</strong> for that property
            group — not a contract rate, not guaranteed, and not a prediction.
            Properties with no invoice attached show “not observed”.
          </div>
        </div>
      ) : null}

      {data.lower_opportunities.length ? (
        <div className="card" style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Priority</th>
                <th>Property / date</th>
                <th>Days out</th>
                <th>Current</th>
                <th>Proposed</th>
                <th>Reduction</th>
                <th>Hist. lead-band ADR</th>
                <th>Gap vs history</th>
                <th>Floors</th>
                <th>Market p25</th>
                <th>Demand</th>
                <th>Observed commission</th>
                <th>Why now</th>
              </tr>
            </thead>
            <tbody>
              {data.lower_opportunities.map((row) => (
                <LowerRow key={row.id} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty message="No price reduction is recommended in the next 60 days." />
      )}
    </>
  )
}
