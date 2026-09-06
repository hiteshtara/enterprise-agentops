import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { OpportunitiesPage } from './OpportunitiesPage'
import { renderWithRouter } from '../test/render'
import { locationSearch } from '../test/location'
import * as api from '../api/agentguard'
import type { RevenueOpportunity, RevenueOpportunityPage } from '../api/types'

vi.mock('../api/agentguard')

function opportunity(over: Partial<RevenueOpportunity> = {}): RevenueOpportunity {
  return {
    id: 'inv-1:2026-10-05',
    listing_id: 'inv-1',
    slug: 'invented',
    display_name: 'Invented Cottage',
    stay_date: '2026-10-05',
    days_out: 30,
    action: 'RAISE',
    current_price: 200,
    proposed_price: 220,
    pct_change: 10,
    confidence: 'HIGH',
    reason: 'below the market p25',
    plain_reason: null,
    plain_action: null,
    refused: null,
    requires_human: false,
    notes: [],
    fingerprint: 'abc123',
    actionable: true,
    blocked_reason: null,
    pricelabs_minimum: null,
    hard_floor: 143,
    normal_floor: 170,
    owner_floor: 143,
    owner_floor_basis: 'flat at the hard floor',
    auto_raise_ceiling: 252,
    absolute_ceiling: 324,
    market_p25: 240,
    market_booked_median: 300,
    market_occupancy: 50,
    listing_occupancy: 70,
    demand: 'Good Demand',
    pickup_7_days: null,
    pinned_price: null,
    events: null,
    market_signal_conflict: false,
    last_refreshed_at: '2026-09-06T10:00:00+00:00',
    stale: false,
    uplift: 20,
    uplift_pct: 10,
    priority: 'REVIEW_NOW',
    priority_reasons: ['$20 a night', 'Normal Demand rather than soft demand'],
    why_now: 'Normal Demand; $220 remains at or below market p25 of $240.',
    is_change_clamped: false,
    ...over,
  }
}

function page(over: Partial<RevenueOpportunityPage> = {}): RevenueOpportunityPage {
  const opportunities = over.opportunities ?? [opportunity()]

  return {
    generated_at: '2026-09-06T12:00:00+00:00',
    horizon_days: 60,
    summary: {
      opportunities: opportunities.length,
      total_uplift: opportunities.reduce((total, row) => total + row.uplift, 0),
      high_confidence: opportunities.filter((r) => r.confidence === 'HIGH').length,
      medium_confidence: opportunities.filter((r) => r.confidence === 'MEDIUM').length,
      properties: new Set(opportunities.map((r) => r.listing_id)).size,
      review_now: opportunities.filter((r) => r.priority === 'REVIEW_NOW').length,
      watch: opportunities.filter((r) => r.priority === 'WATCH').length,
      low_priority: opportunities.filter((r) => r.priority === 'LOW_PRIORITY').length,
    },
    ...over,
    opportunities,
  }
}

/**
 * Rows scoped to the table body. The property filter repeats every display
 * name as an <option>, so an unscoped query matches twice and says nothing
 * about what is actually listed.
 */
function table(container: HTMLElement) {
  return within(container.querySelector('tbody') as HTMLElement)
}

function names(container: HTMLElement): (string | null)[] {
  // Property name is the second cell; priority leads the row.
  return [...container.querySelectorAll('tbody tr td:nth-child(2) strong')].map(
    (cell) => cell.textContent,
  )
}

describe('OpportunitiesPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('offers no way to change a price', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(page())

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    expect(names(container)).toEqual(['Invented Cottage'])

    // The whole point of the page: decision support, not a control surface.
    expect(screen.queryByRole('button', { name: /Review/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Apply/ })).not.toBeInTheDocument()

    expect(api.submitPricingAction).not.toHaveBeenCalled()
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('names the total as a price difference, not expected revenue', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(page())

    renderWithRouter(<OpportunitiesPage />)

    expect(await screen.findByText('Total nightly uplift')).toBeInTheDocument()
    expect(screen.getByText(/Not a revenue forecast/)).toBeInTheDocument()
    expect(screen.getByText(/assumes every night books/)).toBeInTheDocument()
  })

  it('shows the evidence a person needs to judge one night', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({ opportunities: [opportunity({ events: 'Marathon weekend' })] }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    const row = table(container).getAllByRole('row')[0]!

    const cell = within(row)

    expect(cell.getByText('$200')).toBeInTheDocument()
    expect(cell.getByText('$220')).toBeInTheDocument()
    expect(cell.getByText('+$20')).toBeInTheDocument()
    expect(cell.getByText('HIGH')).toBeInTheDocument()
    expect(cell.getByText('$240')).toBeInTheDocument()
    expect(cell.getByText('med $300')).toBeInTheDocument()
    expect(cell.getByText('Good Demand')).toBeInTheDocument()
    expect(cell.getByText('Marathon weekend')).toBeInTheDocument()
    expect(cell.getByText('flat at the hard floor')).toBeInTheDocument()
    expect(cell.getByText('$252')).toBeInTheDocument()
    expect(cell.getByText('none')).toBeInTheDocument()
    expect(cell.getByText('below the market p25')).toBeInTheDocument()
    expect(cell.getByText('2026-10-05')).toBeInTheDocument()
    expect(cell.getByText('30d')).toBeInTheDocument()
  })

  it('sorts by dollar uplift within a priority band', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({ id: 'a', display_name: 'Small', uplift: 10 }),
          opportunity({ id: 'b', display_name: 'Large', uplift: 60 }),
          opportunity({ id: 'c', display_name: 'Middle', uplift: 30 }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    expect(names(container)).toEqual(['Large', 'Middle', 'Small'])
  })

  it('re-sorts on request and keeps the choice in the URL', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({
            id: 'a',
            display_name: 'Later',
            stay_date: '2026-11-01',
            uplift: 60,
          }),
          opportunity({
            id: 'b',
            display_name: 'Sooner',
            stay_date: '2026-09-20',
            uplift: 10,
          }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    await userEvent.setup().selectOptions(screen.getByLabelText(/Sort by/), 'date')

    expect(names(container)).toEqual(['Sooner', 'Later'])
    expect(locationSearch()).toContain('sort=date')
  })

  it('filters by lead-time window and reconciles the total to what is shown', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({ id: 'a', display_name: 'Soon', days_out: 3, uplift: 10 }),
          opportunity({ id: 'b', display_name: 'Far', days_out: 45, uplift: 60 }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    await userEvent.setup().selectOptions(screen.getByLabelText(/Days out/), '0-7')

    expect(names(container)).toEqual(['Soon'])

    // The headline total stays whole; the filtered total is stated separately
    // so a reader can always add up the column in front of them.
    expect(screen.getByText(/Showing 1 of 2/)).toBeInTheDocument()
    expect(screen.getByText(/\$10 of \$70 nightly uplift/)).toBeInTheDocument()
    expect(locationSearch()).toContain('window=0-7')
  })

  it('filters by confidence', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({ id: 'a', display_name: 'Sure', confidence: 'HIGH' }),
          opportunity({ id: 'b', display_name: 'Less sure', confidence: 'MEDIUM' }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    expect(names(container)).toHaveLength(2)

    await userEvent.setup().selectOptions(screen.getByLabelText(/Confidence/), 'HIGH')

    expect(names(container)).toEqual(['Sure'])
  })

  it('says plainly when there is nothing to do', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({ opportunities: [] }),
    )

    renderWithRouter(<OpportunitiesPage />)

    expect(
      await screen.findByText('No raise is recommended in the next 60 days.'),
    ).toBeInTheDocument()
  })

  it('reports a provider failure without leaking internals', async () => {
    const { ApiError } = await import('../api/client')

    vi.mocked(api.getRevenueOpportunities).mockRejectedValue(
      new ApiError('Pricing recommendations could not be built.', 502),
    )

    renderWithRouter(<OpportunitiesPage />)

    expect(
      await screen.findByText('Pricing recommendations could not be built.'),
    ).toBeInTheDocument()
  })

  it('renders a priority badge on every row', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({ id: 'a', display_name: 'Now', priority: 'REVIEW_NOW' }),
          opportunity({ id: 'b', display_name: 'Later', priority: 'WATCH' }),
          opportunity({ id: 'c', display_name: 'Filed', priority: 'LOW_PRIORITY' }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    const badges = [...container.querySelectorAll('tbody tr td:first-child .badge')]

    expect(badges.map((b) => b.textContent)).toEqual([
      'Review now',
      'Watch',
      'Low priority',
    ])
  })

  it('counts each priority band in the summary tiles', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({ id: 'a', priority: 'REVIEW_NOW' }),
          opportunity({ id: 'b', priority: 'WATCH', stay_date: '2026-10-06' }),
          opportunity({ id: 'c', priority: 'WATCH', stay_date: '2026-10-07' }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    const tiles = within(container.querySelector('.grid-stats') as HTMLElement)

    expect(tiles.getByText('Review now')).toBeInTheDocument()
    expect(tiles.getByText('Watch')).toBeInTheDocument()
    expect(tiles.getByText('Low priority')).toBeInTheDocument()
  })

  it('orders REVIEW_NOW then WATCH then LOW_PRIORITY by default', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({
            id: 'a',
            display_name: 'Filed',
            priority: 'LOW_PRIORITY',
            uplift: 90,
          }),
          opportunity({
            id: 'b',
            display_name: 'Later',
            priority: 'WATCH',
            uplift: 80,
          }),
          opportunity({
            id: 'c',
            display_name: 'Now',
            priority: 'REVIEW_NOW',
            uplift: 20,
          }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    // Band beats dollars: the biggest number is filed last.
    expect(names(container)).toEqual(['Now', 'Later', 'Filed'])
  })

  it('sorts by uplift descending inside a priority band', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({
            id: 'a',
            display_name: 'Small',
            priority: 'REVIEW_NOW',
            uplift: 15,
          }),
          opportunity({
            id: 'b',
            display_name: 'Large',
            priority: 'REVIEW_NOW',
            uplift: 60,
          }),
          opportunity({
            id: 'c',
            display_name: 'Watched',
            priority: 'WATCH',
            uplift: 99,
          }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    expect(names(container)).toEqual(['Large', 'Small', 'Watched'])
  })

  it('filters by priority', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({ id: 'a', display_name: 'Now', priority: 'REVIEW_NOW' }),
          opportunity({ id: 'b', display_name: 'Later', priority: 'WATCH' }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    await userEvent
      .setup()
      .selectOptions(screen.getByLabelText(/Priority/), 'REVIEW_NOW')

    expect(names(container)).toEqual(['Now'])
    expect(locationSearch()).toContain('priority=REVIEW_NOW')
  })

  it('shows why now, and the cap indicator only when the move was capped', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(
      page({
        opportunities: [
          opportunity({
            id: 'a',
            display_name: 'Capped',
            why_now: 'Low Demand; unit occupancy is 58% vs market 21%.',
            is_change_clamped: true,
          }),
          opportunity({
            id: 'b',
            display_name: 'Free',
            stay_date: '2026-10-09',
            why_now: 'Normal Demand; the case rests on the uplift alone.',
            is_change_clamped: false,
          }),
        ],
      }),
    )

    const { container } = renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    expect(
      screen.getByText('Low Demand; unit occupancy is 58% vs market 21%.'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Normal Demand; the case rests on the uplift alone.'),
    ).toBeInTheDocument()

    // Exactly one row is marked capped.
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2)
    expect(screen.getAllByText('capped at 10%')).toHaveLength(1)
  })

  it('still offers no control that could change a price', async () => {
    vi.mocked(api.getRevenueOpportunities).mockResolvedValue(page())

    renderWithRouter(<OpportunitiesPage />)

    await screen.findByText('Total nightly uplift')

    for (const label of [/Review/, /Approve/, /Apply/, /Reject/]) {
      expect(screen.queryByRole('button', { name: label })).not.toBeInTheDocument()
    }

    expect(api.submitPricingAction).not.toHaveBeenCalled()
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })
})
