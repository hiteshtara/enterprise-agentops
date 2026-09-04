import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RecommendedActions } from './RecommendedActions'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type { PricingRecommendation, PricingRecommendationPage } from '../api/types'

vi.mock('../api/agentguard')

function rec(over: Partial<PricingRecommendation> = {}): PricingRecommendation {
  return {
    id: 'inv-1:2026-09-20',
    listing_id: 'inv-1',
    slug: 'invented',
    display_name: 'Invented Cottage',
    stay_date: '2026-09-20',
    days_out: 16,
    action: 'LOWER',
    current_price: 200,
    proposed_price: 190,
    pct_change: -5,
    confidence: 'HIGH',
    reason: 'near-term vacancy',
    refused: null,
    requires_human: false,
    notes: [],
    fingerprint: 'abc123',
    actionable: true,
    blocked_reason: null,
    pricelabs_minimum: 143,
    hard_floor: 143,
    normal_floor: 170,
    auto_raise_ceiling: 252,
    absolute_ceiling: 324,
    market_p25: 240,
    market_booked_median: 300,
    market_occupancy: 50,
    listing_occupancy: 70,
    demand: 'Low Demand',
    pickup_7_days: 9,
    pinned_price: null,
    last_refreshed_at: '2026-09-04T11:00:00+00:00',
    ...over,
  }
}

function page(over: Partial<PricingRecommendationPage> = {}): PricingRecommendationPage {
  return {
    generated_at: '2026-09-04T12:00:00+00:00',
    horizon_days: 60,
    writes_enabled: true,
    max_change_per_run: 0.1,
    recommendations: [rec()],
    bands: [],
    ...over,
  }
}

const approval = {
  approval_id: 'ap-1',
  run_id: 'run-1',
  tool: 'apply_pricing_action',
  arguments: {},
  risk: 'DANGEROUS',
}

describe('RecommendedActions', () => {
  beforeEach(() => vi.resetAllMocks())

  it('shows only actionable recommendations', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(
      page({
        recommendations: [
          rec(),
          rec({ id: 'x', action: 'HOLD', actionable: false, display_name: 'Held Home' }),
        ],
      }),
    )

    renderWithRouter(<RecommendedActions />)

    expect(await screen.findByText('Invented Cottage')).toBeInTheDocument()
    expect(screen.queryByText('Held Home')).not.toBeInTheDocument()
  })

  it('offers no approve control until the change has been reviewed', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(page())

    renderWithRouter(<RecommendedActions />)

    await screen.findByText('Invented Cottage')

    expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Approve/ }),
    ).not.toBeInTheDocument()
  })

  it('reveals every guardrail before approval is possible', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(page())
    vi.mocked(api.submitPricingAction).mockResolvedValue({
      run_id: 'run-1',
      status: 'WAITING_FOR_APPROVAL',
      answer: '',
      trace: [],
      approval_required: approval,
    } as never)

    renderWithRouter(<RecommendedActions />)

    await userEvent.setup().click(await screen.findByRole('button', { name: 'Review' }))

    expect(await screen.findByText('Hard floor')).toBeInTheDocument()
    expect(screen.getByText('Auto-raise ceiling')).toBeInTheDocument()
    expect(screen.getByText('Absolute ceiling')).toBeInTheDocument()
    expect(screen.getByText('Market p25')).toBeInTheDocument()
    expect(screen.getByText('State fingerprint')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Approve & Apply/ })).toBeInTheDocument()
  })

  it('reports a stored override without claiming the channel price changed', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(page())
    vi.mocked(api.submitPricingAction).mockResolvedValue({
      run_id: 'run-1',
      status: 'WAITING_FOR_APPROVAL',
      answer: '',
      trace: [],
      approval_required: approval,
    } as never)
    vi.mocked(api.resolveApproval).mockResolvedValue({
      approval_id: 'ap-1',
      approved: true,
      tool: 'apply_pricing_action',
      result: {
        outcome: 'CONFIRMED_APPLIED',
        stay_date: '2026-09-20',
        old_price: 200,
        new_price: 190,
        message: 'applied',
        needs_human: false,
      },
      run_id: 'run-1',
      run_status: 'COMPLETED',
      answer: '',
      trace: [],
    } as never)

    const user = userEvent.setup()

    renderWithRouter(<RecommendedActions />)

    await user.click(await screen.findByRole('button', { name: 'Review' }))
    await user.click(await screen.findByRole('button', { name: /Approve & Apply/ }))

    expect(await screen.findByText('Override stored.')).toBeInTheDocument()
    expect(
      screen.getByText(/not confirmation that it has/),
    ).toBeInTheDocument()
  })

  it('an unknown write state offers no retry and says to check PriceLabs', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(page())
    vi.mocked(api.submitPricingAction).mockResolvedValue({
      run_id: 'run-1',
      status: 'WAITING_FOR_APPROVAL',
      answer: '',
      trace: [],
      approval_required: approval,
    } as never)
    vi.mocked(api.resolveApproval).mockResolvedValue({
      approval_id: 'ap-1',
      approved: true,
      tool: 'apply_pricing_action',
      result: {
        outcome: 'UNKNOWN_WRITE_STATE',
        stay_date: '2026-09-20',
        message: 'ambiguous',
        needs_human: true,
      },
      run_id: 'run-1',
      run_status: 'COMPLETED',
      answer: '',
      trace: [],
    } as never)

    const user = userEvent.setup()

    renderWithRouter(<RecommendedActions />)

    await user.click(await screen.findByRole('button', { name: 'Review' }))
    await user.click(await screen.findByRole('button', { name: /Approve & Apply/ }))

    expect(
      await screen.findByText(/check PriceLabs before doing anything else/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
  })

  it('rejecting changes nothing and says so', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(page())
    vi.mocked(api.submitPricingAction).mockResolvedValue({
      run_id: 'run-1',
      status: 'WAITING_FOR_APPROVAL',
      answer: '',
      trace: [],
      approval_required: approval,
    } as never)
    vi.mocked(api.resolveApproval).mockResolvedValue({
      approval_id: 'ap-1',
      approved: false,
      tool: 'apply_pricing_action',
      result: null,
      run_id: 'run-1',
      run_status: 'CANCELLED',
      answer: '',
      trace: [],
    } as never)

    const user = userEvent.setup()

    renderWithRouter(<RecommendedActions />)

    await user.click(await screen.findByRole('button', { name: 'Review' }))
    await user.click(await screen.findByRole('button', { name: 'Reject' }))

    expect(await screen.findByText(/Nothing was changed/)).toBeInTheDocument()
  })

  it('says plainly when pricing writes are disabled', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(
      page({ writes_enabled: false }),
    )

    renderWithRouter(<RecommendedActions />)

    expect(await screen.findByText('Pricing writes are disabled.')).toBeInTheDocument()
  })

  it('blocks a recommendation whose provider behaviour is unverified', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(
      page({
        recommendations: [
          rec({ blocked_reason: 'lead_time_expiry has not been verified' }),
        ],
      }),
    )

    renderWithRouter(<RecommendedActions />)

    expect(
      await screen.findByText('Blocked pending live verification.'),
    ).toBeInTheDocument()
    // No path to approval exists while it is blocked.
    expect(screen.queryByRole('button', { name: 'Review' })).not.toBeInTheDocument()
  })

  it('marks a recommendation that always needs a human', async () => {
    vi.mocked(api.getPricingRecommendations).mockResolvedValue(
      page({ recommendations: [rec({ action: 'RAISE', requires_human: true })] }),
    )

    renderWithRouter(<RecommendedActions />)

    expect(
      await screen.findByText('Always requires a human decision.'),
    ).toBeInTheDocument()
  })
})
