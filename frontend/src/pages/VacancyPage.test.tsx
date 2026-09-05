import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { VacancyPage } from './VacancyPage'
import { renderWithRouter } from '../test/render'
import { locationSearch } from '../test/location'
import * as api from '../api/agentguard'
import type { VacancyBoard, VacancyResponse } from '../api/types'

vi.mock('../api/agentguard')

function connected(board: VacancyBoard): VacancyResponse {
  return { configured: true, message: null, board }
}

function board(overrides: Partial<VacancyBoard> = {}): VacancyBoard {
  return {
    horizon_days: 60,
    start_date: '2026-09-07',
    end_date: '2026-11-05',
    source: 'PriceLabs (fixtures)',
    source_is_live: false,
    generated_from_nights: 4,
    summary: {
      properties: 1,
      nights_counted: 4,
      nights_missing: 0,
      booked_nights: 1,
      open_sellable_nights: 1,
      unbookable_nights: 1,
      blocked_nights: 0,
      unknown_nights: 1,
      occupancy_pct: 33.3,
      sellable_gross_value: 220,
      unbookable_gross_value: 740,
    },
    properties: [
      {
        listing_id: 'inv-1',
        display_name: 'Invented Cottage',
        currency: 'USD',
        last_refreshed_at: new Date().toISOString(),
        nights_counted: 4,
        nights_missing: 0,
        booked_nights: 1,
        open_sellable_nights: 1,
        unbookable_nights: 1,
        blocked_nights: 0,
        unknown_nights: 1,
        occupancy_pct: 33.3,
        sellable_gross_value: 220,
        sellable_priced_nights: 1,
        unbookable_gross_value: 740,
        high_value_threshold: 500,
        median_price: 300,
        market_occupancy_pct: 35,
        listing_occupancy_pct: 19,
        booking_window_min_days: 4,
        booking_window_max_days: 47,
        provider_flag: null,
        provider_recommendations: [],
        calendar: [
          {
            stay_date: '2026-09-07',
            state: 'BOOKED',
            price: 200,
            minimum_stay: 3,
            is_weekend: false,
          },
          {
            stay_date: '2026-09-08',
            state: 'OPEN',
            price: 220,
            minimum_stay: 3,
            is_weekend: false,
          },
          {
            stay_date: '2026-09-09',
            state: 'UNBOOKABLE',
            price: 740,
            minimum_stay: 3,
            is_weekend: false,
          },
          {
            stay_date: '2026-09-10',
            state: 'UNKNOWN',
            price: null,
            minimum_stay: null,
            is_weekend: false,
          },
        ],
      },
    ],
    unbookable_windows: [
      {
        listing_id: 'inv-1',
        display_name: 'Invented Cottage',
        start: '2026-09-09',
        end: '2026-09-09',
        nights: 1,
        weekend_nights: 0,
        priced_nights: 1,
        gross_value: 740,
        adr: 740,
        high_value_nights: 1,
        minimum_stays: [3],
        truncated: false,
        complete_pricing: true,
        reason: '3-night minimum against a 1-night gap',
        orphan_class: 'one_night',
        high_value: true,
        prices: [740],
        reasons: [],
      },
    ],
    open_windows: [],
    high_value_nights: [],
    needs_attention: [
      {
        listing_id: 'inv-1',
        display_name: 'Invented Cottage',
        reasons: ['October: occupancy 19% against a market at 35%'],
        below_market: true,
        occupancy_gap_points: 16,
        listing_occupancy_pct: 19,
        market_occupancy_pct: 35,
        month_label: 'October',
        provider_recommendations: [],
      },
    ],
    opportunities: [
      {
        listing_id: 'inv-1',
        display_name: 'Invented Cottage',
        start: '2026-09-08',
        end: '2026-09-08',
        nights: 1,
        weekend_nights: 0,
        priced_nights: 1,
        gross_value: 220,
        adr: 220,
        high_value_nights: 0,
        minimum_stays: [3],
        truncated: false,
        complete_pricing: true,
        rank: 1,
        score: 220,
        reasons: ['$220 open value', 'occupancy below market'],
        lead_days: 1,
      },
    ],
    ...overrides,
  }
}

describe('VacancyPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('summarises open, booked, unbookable and trapped value separately', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(board()))

    const { container } = renderWithRouter(<VacancyPage />)

    await screen.findByText('Open sellable nights')

    // Scoped to the summary tiles. The same figures reappear per property
    // below, which is the point -- one number described twice, never summed.
    const tiles = within(container.querySelector('.grid-stats') as HTMLElement)

    expect(tiles.getByText('Unbookable nights')).toBeInTheDocument()
    expect(tiles.getByText('Value trapped')).toBeInTheDocument()
    expect(tiles.getByText('$740')).toBeInTheDocument()
    expect(tiles.getByText('$220')).toBeInTheDocument()
  })

  it('shows unbookable gaps with the reason and the minimum stay', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(board()))

    renderWithRouter(<VacancyPage />)

    expect(
      await screen.findByText('3-night minimum against a 1-night gap'),
    ).toBeInTheDocument()
    expect(screen.getByText('high value')).toBeInTheDocument()
  })

  it('names why each opportunity ranked', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(board()))

    renderWithRouter(<VacancyPage />)

    const table = within(await screen.findByRole('table'))

    expect(table.getByText('$220 open value')).toBeInTheDocument()
    expect(table.getByText('occupancy below market')).toBeInTheDocument()
  })

  it('surfaces a property below its market under Needs attention', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(board()))

    renderWithRouter(<VacancyPage />)

    expect(
      await screen.findByText('October: occupancy 19% against a market at 35%'),
    ).toBeInTheDocument()
    expect(screen.getByText('16 pts below market')).toBeInTheDocument()
  })

  it('renders an unknown night as unknown, never as open', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(board()))

    const { container } = renderWithRouter(<VacancyPage />)

    await screen.findByText('Property calendars')

    // One cell per night in the strip, plus the five legend swatches.
    expect(container.querySelectorAll('.vac-strip .vac-unknown')).toHaveLength(1)
    expect(container.querySelectorAll('.vac-strip .vac-open')).toHaveLength(1)
    expect(container.querySelectorAll('.vac-strip .vac-unbookable')).toHaveLength(1)
  })

  it('refetches when the horizon changes and keeps it in the URL', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(board()))

    renderWithRouter(<VacancyPage />)

    await screen.findByText('Open sellable nights')

    expect(api.getVacancyBoard).toHaveBeenCalledWith(60)

    await userEvent.setup().click(screen.getByRole('button', { name: '7 days' }))

    await waitFor(() => expect(api.getVacancyBoard).toHaveBeenCalledWith(7))

    // MemoryRouter never touches window.location, so the router's own view of
    // the URL is what proves the filter is shareable.
    expect(locationSearch()).toContain('days=7')
  })

  it('shows a connection notice, and no numbers, when PriceLabs is absent', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue({
      configured: false,
      message: 'PriceLabs is not connected to AgentGuard yet.',
      board: null,
    })

    const { container } = renderWithRouter(<VacancyPage />)

    expect(
      await screen.findByText('PriceLabs is not connected to AgentGuard yet.'),
    ).toBeInTheDocument()

    // Nothing that could be mistaken for real inventory.
    expect(container.querySelector('.grid-stats')).toBeNull()
    expect(container.querySelectorAll('.vac-section')).toHaveLength(0)
    expect(container.querySelectorAll('.vac-strip')).toHaveLength(0)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByText(/PRICELABS_API_KEY/)).toBeInTheDocument()
  })

  it('still offers the horizon controls while unconfigured', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue({
      configured: false,
      message: null,
      board: null,
    })

    renderWithRouter(<VacancyPage />)

    await screen.findByRole('status')

    expect(screen.getByRole('button', { name: '60 days' })).toBeInTheDocument()
  })

  it('does not label a property that is ahead of its market as behind it', async () => {
    const ahead = board()

    ahead.needs_attention = [
      {
        listing_id: 'inv-2',
        display_name: 'Ahead Of Market',
        reasons: ['6 consecutive open nights inside the booking window'],
        below_market: false,
        // Negative: this property is 17 points *above* its market.
        occupancy_gap_points: -17,
        listing_occupancy_pct: 61,
        market_occupancy_pct: 44,
        month_label: 'October',
        provider_recommendations: [],
      },
    ]

    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(ahead))

    renderWithRouter(<VacancyPage />)

    await screen.findByText('Ahead Of Market')

    // The badge reads "<n> pts below market"; the opportunity chips carry
    // "occupancy below market" and are a different claim about a different row.
    expect(screen.queryByText(/pts below market/)).not.toBeInTheDocument()
  })

  it('shows PriceLabs freshness and does not claim to be live', async () => {
    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(board()))

    renderWithRouter(<VacancyPage />)

    expect(
      await screen.findByText(/PriceLabs data last refreshed:/),
    ).toBeInTheDocument()
    expect(screen.getByText(/mirrors the PMS/)).toBeInTheDocument()
  })

  it('flags a stale refresh rather than presenting it as current', async () => {
    const old = new Date(Date.now() - 40 * 3_600_000).toISOString()

    const stale = board()

    stale.properties[0].last_refreshed_at = old

    vi.mocked(api.getVacancyBoard).mockResolvedValue(connected(stale))

    renderWithRouter(<VacancyPage />)

    expect(await screen.findByText(/older than a day/)).toBeInTheDocument()
  })

  it('reports a provider failure without leaking internals', async () => {
    const { ApiError } = await import('../api/client')

    vi.mocked(api.getVacancyBoard).mockRejectedValue(
      new ApiError('Vacancy data could not be loaded from the provider.', 502),
    )

    renderWithRouter(<VacancyPage />)

    expect(
      await screen.findByText('Vacancy data could not be loaded from the provider.'),
    ).toBeInTheDocument()
  })
})
