import { getVacancyBoard } from '../api/agentguard'
import type {
  NightState,
  VacancyAttention,
  VacancyBoard,
  VacancyNight,
  VacancyProperty,
  VacancyWindow,
} from '../api/types'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'
import { RecommendedActions } from '../components/RecommendedActions'
import { useAsync } from '../hooks/useAsync'
import { useUrlFilter } from '../hooks/useUrlFilter'

/**
 * Where there is open inventory, what it is worth, and where to look.
 *
 * Read-only, by construction as well as by intent: this page calls one GET and
 * the backend exposes no route that changes a price, a stay restriction,
 * availability or a reservation. There is no control here that could.
 *
 * Two distinctions the page refuses to blur, because blurring them is how a
 * board like this starts lying:
 *
 *   * **Open is not the same as sellable.** Only `OPEN` nights count towards
 *     open inventory. `UNBOOKABLE` nights are vacant and cannot be booked;
 *     they get their own section rather than padding the headline number.
 *   * **Unknown is not open.** A night whose state the provider did not
 *     establish is rendered as unknown and excluded from every total.
 */
const HORIZONS = ['7', '30', '60'] as const

type Horizon = (typeof HORIZONS)[number]

const DEFAULT_HORIZON: Horizon = '60'

const STATE_LABEL: Record<NightState, string> = {
  BOOKED: 'Booked',
  OPEN: 'Open',
  UNBOOKABLE: 'Unbookable',
  BLOCKED: 'Blocked by owner',
  UNKNOWN: 'Unknown',
}

/** Older than this and the board says so rather than implying it is current. */
const STALE_AFTER_HOURS = 24

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'

  return `$${Math.round(value).toLocaleString()}`
}

function percent(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(0)}%`
}

function shortDate(iso: string): string {
  const [, month, day] = iso.split('-')

  const months = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ')

  return `${months[Number(month) - 1]} ${Number(day)}`
}

function range(window: VacancyWindow): string {
  return window.start === window.end
    ? shortDate(window.start)
    : `${shortDate(window.start)} – ${shortDate(window.end)}`
}

function nightsLabel(count: number): string {
  return count === 1 ? '1 night' : `${count} nights`
}

/** Hours since a timestamp, or null when it cannot be read. */
function hoursSince(iso: string | null): number | null {
  if (!iso) return null

  const then = Date.parse(iso)

  if (Number.isNaN(then)) return null

  return (Date.now() - then) / 3_600_000
}

function freshnessLine(properties: VacancyProperty[]): {
  text: string
  stale: boolean
} {
  const ages = properties
    .map((entry) => hoursSince(entry.last_refreshed_at))
    .filter((age): age is number => age !== null)

  if (!ages.length) {
    return { text: 'PriceLabs refresh time unknown', stale: true }
  }

  const oldest = Math.max(...ages)

  const stamps = properties
    .map((entry) => entry.last_refreshed_at)
    .filter((stamp): stamp is string => Boolean(stamp))
    .sort()

  const at = new Date(stamps[0])

  const sameDay = at.toDateString() === new Date().toDateString()

  // A bare clock time makes a day-old stamp read as this morning, so anything
  // that is not from today carries its date.
  const when = sameDay
    ? at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : at.toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      })

  const label =
    properties.length > 1 ? 'Oldest PriceLabs refresh' : 'PriceLabs data last refreshed'

  return {
    text: `${label}: ${when}`,
    stale: oldest > STALE_AFTER_HOURS,
  }
}

function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: string
  tone?: 'ok' | 'warn' | 'danger'
  hint?: string
}) {
  return (
    <div className="card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone ? ` tone-${tone}` : ''}`}>{value}</div>
      {hint ? <div className="metric-hint">{hint}</div> : null}
    </div>
  )
}

function CalendarStrip({ nights }: { nights: VacancyNight[] }) {
  return (
    <div className="vac-strip" role="img" aria-label="Nightly availability">
      {nights.map((night) => (
        <span
          key={night.stay_date}
          className={`vac-cell vac-${night.state.toLowerCase()}${
            night.is_weekend ? ' vac-weekend' : ''
          }`}
          title={`${night.stay_date} · ${STATE_LABEL[night.state]}${
            night.price !== null ? ` · $${Math.round(night.price)}` : ''
          }${night.minimum_stay ? ` · min stay ${night.minimum_stay}` : ''}`}
        />
      ))}
    </div>
  )
}

function Legend() {
  const items: [NightState, string][] = [
    ['BOOKED', 'Booked'],
    ['OPEN', 'Open & sellable'],
    ['UNBOOKABLE', 'Unbookable'],
    ['BLOCKED', 'Blocked'],
    ['UNKNOWN', 'Unknown'],
  ]

  return (
    <div className="vac-legend">
      {items.map(([state, label]) => (
        <span key={state}>
          <span className={`vac-cell vac-${state.toLowerCase()}`} /> {label}
        </span>
      ))}
      <span className="faint">Weekend nights carry a marker.</span>
    </div>
  )
}

function OrphanRow({ window: gap }: { window: VacancyWindow }) {
  const tone = gap.orphan_class === 'one_night' ? 'tone-warn' : 'tone-neutral'

  return (
    <div className="card vac-orphan">
      <div className="vac-head">
        <div>
          <strong>{gap.display_name}</strong>{' '}
          <span className="mono muted">{range(gap)}</span>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <span className={`badge ${tone}`}>
            <span className="badge-dot" aria-hidden="true" />
            {nightsLabel(gap.nights)}
          </span>
          {gap.high_value ? (
            <span className="badge tone-danger">
              <span className="badge-dot" aria-hidden="true" />
              high value
            </span>
          ) : null}
        </div>
      </div>

      <div className="vac-orphan-body">
        <div>
          <div className="stat-label">Trapped value</div>
          <div className="vac-figure">{money(gap.gross_value)}</div>
        </div>
        <div>
          <div className="stat-label">Nightly</div>
          <div className="mono">
            {(gap.prices ?? []).map((price, index) => (
              <span key={index}>
                {index ? ' · ' : ''}
                {price === null ? '—' : `$${Math.round(price)}`}
              </span>
            ))}
          </div>
        </div>
        <div>
          <div className="stat-label">Minimum stay</div>
          <div className="mono">
            {gap.minimum_stays.length ? gap.minimum_stays.join(', ') : '—'}
          </div>
        </div>
      </div>

      {gap.reason ? <p className="vac-reason">{gap.reason}</p> : null}
    </div>
  )
}

function AttentionRow({ entry }: { entry: VacancyAttention }) {
  return (
    <div className="card">
      <div className="vac-head">
        <strong>{entry.display_name}</strong>
        {entry.below_market && entry.occupancy_gap_points !== null ? (
          <span className="badge tone-warn">
            <span className="badge-dot" aria-hidden="true" />
            {entry.occupancy_gap_points.toFixed(0)} pts below market
          </span>
        ) : null}
      </div>
      <ul className="vac-reasons">
        {entry.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
    </div>
  )
}

function OpportunityRow({ window: entry }: { window: VacancyWindow }) {
  return (
    <tr>
      <td className="mono muted">{entry.rank}</td>
      <td>
        <strong>{entry.display_name}</strong>
      </td>
      <td className="mono">
        {range(entry)}
        {entry.truncated ? (
          <span className="faint" title="Window continues past the horizon">
            {' '}
            *
          </span>
        ) : null}
      </td>
      <td className="mono vac-num">{entry.nights}</td>
      <td className="mono vac-num">{money(entry.gross_value)}</td>
      <td className="mono vac-num">{money(entry.adr)}</td>
      <td>
        <div className="vac-why">
          {entry.reasons.map((reason) => (
            <span key={reason} className="vac-chip">
              {reason}
            </span>
          ))}
        </div>
      </td>
    </tr>
  )
}

function PropertyCard({ property }: { property: VacancyProperty }) {
  const refreshed = property.last_refreshed_at
    ? new Date(property.last_refreshed_at).toLocaleTimeString([], {
        hour: 'numeric',
        minute: '2-digit',
      })
    : 'unknown'

  return (
    <div className="card">
      <div className="vac-head">
        <div>
          <div className="card-title">{property.display_name}</div>
          <div className="faint mono">
            {property.listing_id} · refreshed {refreshed}
          </div>
        </div>
        <div className="vac-head-figures">
          <div>
            <div className="stat-label">Occupancy</div>
            <div className="vac-figure">{percent(property.occupancy_pct)}</div>
          </div>
          <div>
            <div className="stat-label">Open value</div>
            <div className="vac-figure">{money(property.sellable_gross_value)}</div>
          </div>
        </div>
      </div>

      <CalendarStrip nights={property.calendar} />

      <div className="vac-counts">
        <span>{property.booked_nights} booked</span>
        <span>{property.open_sellable_nights} open</span>
        {property.unbookable_nights ? (
          <span className="tone-text-danger">
            {property.unbookable_nights} unbookable
          </span>
        ) : null}
        {property.blocked_nights ? (
          <span>{property.blocked_nights} blocked</span>
        ) : null}
        {property.unknown_nights ? (
          <span className="muted">{property.unknown_nights} unknown</span>
        ) : null}
        {property.nights_missing ? (
          <span className="muted">{property.nights_missing} not returned</span>
        ) : null}
        {property.high_value_threshold !== null ? (
          <span className="faint">
            high value ≥ ${Math.round(property.high_value_threshold)}
          </span>
        ) : null}
      </div>

      {property.provider_recommendations.length ? (
        <p className="vac-reason">
          PriceLabs recommends: {property.provider_recommendations.join('; ')}
        </p>
      ) : null}
    </div>
  )
}

function Board({ board }: { board: VacancyBoard }) {
  const { summary } = board

  const freshness = freshnessLine(board.properties)

  return (
    <>
      <p className={`vac-freshness${freshness.stale ? ' tone-text-danger' : ''}`}>
        {freshness.text}
        {freshness.stale ? ' — this is older than a day' : null}
        <span className="faint">
          {' '}
          · {board.source} mirrors the PMS, so it is not live availability
        </span>
      </p>

      <div className="grid grid-stats">
        <Stat
          label="Open sellable nights"
          value={String(summary.open_sellable_nights)}
          hint={`of ${summary.nights_counted} nights`}
        />
        <Stat label="Booked nights" value={String(summary.booked_nights)} />
        <Stat label="Occupancy" value={percent(summary.occupancy_pct)} />
        <Stat
          label="Open value"
          value={money(summary.sellable_gross_value)}
          tone="ok"
          hint="sellable nights only"
        />
        <Stat
          label="Unbookable nights"
          value={String(summary.unbookable_nights)}
          tone={summary.unbookable_nights ? 'danger' : undefined}
          hint="vacant, cannot be booked"
        />
        <Stat
          label="Value trapped"
          value={money(summary.unbookable_gross_value)}
          tone={summary.unbookable_nights ? 'danger' : undefined}
        />
      </div>

      <RecommendedActions />

      <section className="vac-section">
        <h2 className="card-title">Unbookable gaps</h2>
        <p className="page-subtitle">
          Vacant nights a guest cannot book, because a stay restriction blocks them.
          Surfaced only — nothing here changes a minimum stay.
        </p>
        {board.unbookable_windows.length ? (
          <div className="grid">
            {board.unbookable_windows.map((gap) => (
              <OrphanRow key={`${gap.listing_id}-${gap.start}`} window={gap} />
            ))}
          </div>
        ) : (
          <Empty message="No unbookable vacant nights in this horizon." />
        )}
      </section>

      <section className="vac-section">
        <h2 className="card-title">Needs attention</h2>
        <p className="page-subtitle">
          Drawn from PriceLabs pacing and market data. Any pricing advice shown here is
          PriceLabs' own recommendation, quoted.
        </p>
        {board.needs_attention.length ? (
          <div className="grid">
            {board.needs_attention.map((entry) => (
              <AttentionRow key={entry.listing_id} entry={entry} />
            ))}
          </div>
        ) : (
          <Empty message="No property is materially below its market in this horizon." />
        )}
      </section>

      <section className="vac-section">
        <h2 className="card-title">Top opportunities</h2>
        <p className="page-subtitle">
          Ranked by a fixed formula over open value, weekend nights, property-relative
          high-value nights, booking-window pacing and market position. No model scores
          these.
        </p>
        {board.opportunities.length ? (
          <div className="vac-table-wrap">
            <table className="vac-table">
              <thead>
                <tr>
                  <th />
                  <th>Property</th>
                  <th>Window</th>
                  <th className="vac-num">Nights</th>
                  <th className="vac-num">Value</th>
                  <th className="vac-num">ADR</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {board.opportunities.map((entry) => (
                  <OpportunityRow
                    key={`${entry.listing_id}-${entry.start}`}
                    window={entry}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty message="No priced open windows to rank in this horizon." />
        )}
      </section>

      <section className="vac-section">
        <h2 className="card-title">Property calendars</h2>
        <Legend />
        <div className="grid">
          {board.properties.map((property) => (
            <PropertyCard key={property.listing_id} property={property} />
          ))}
        </div>
      </section>
    </>
  )
}

/**
 * What the page shows when PriceLabs is not connected.
 *
 * Deliberately empty of numbers. An unconfigured connector used to render a
 * sample portfolio here, which read on screen exactly like a real one -- the
 * reader had no way to tell invented inventory from their own. No totals, no
 * properties, no opportunities: just what is missing and how to supply it.
 */
function NotConnected({ message }: { message: string | null }) {
  return (
    <div className="card vac-notconnected" role="status">
      <h2 className="card-title">
        {message ?? 'PriceLabs is not connected to AgentGuard yet.'}
      </h2>
      <p>
        This page reads open inventory, nightly pricing and pacing from PriceLabs. Until
        an account is connected there is nothing to show, and AgentGuard will not stand
        in sample properties for real ones.
      </p>
      <p className="faint">
        Set <code className="mono">PRICELABS_API_KEY</code> from PriceLabs Account
        Settings → API Details, then restart AgentGuard.
      </p>
    </div>
  )
}

export function VacancyPage() {
  const [raw, setHorizon] = useUrlFilter<Horizon>('days', HORIZONS)

  const horizon: Horizon = raw || DEFAULT_HORIZON

  const { data, error, loading } = useAsync(
    () => getVacancyBoard(Number(horizon)),
    [horizon],
  )

  return (
    <>
      <PageHeader
        title="Vacancy"
        subtitle="Where you have open inventory, what it is worth, and where to look."
        actions={
          <div className="row" role="group" aria-label="Horizon">
            {HORIZONS.map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={option === horizon}
                className={option === horizon ? 'vac-horizon active' : 'vac-horizon'}
                onClick={() => setHorizon(option)}
              >
                {option} days
              </button>
            ))}
          </div>
        }
      />

      {loading ? <Loading label="Loading vacancy board" /> : null}
      {error ? <ErrorState error={error} /> : null}
      {data && !data.configured ? <NotConnected message={data.message} /> : null}
      {data?.configured && data.board ? <Board board={data.board} /> : null}
    </>
  )
}
