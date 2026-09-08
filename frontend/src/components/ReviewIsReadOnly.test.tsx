import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RecommendedActions } from './RecommendedActions'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type { PricingRecommendation, PricingRecommendationPage } from '../api/types'

vi.mock('../api/agentguard')

/**
 * A deployment that permits live pricing, with a session covering the fixture
 * listing. Needed wherever a test exercises the write path: a card only offers
 * approval when the deployment controls *and* an active session agree.
 */
function liveSession(over: Record<string, unknown> = {}) {
  vi.mocked(api.getPricingSession).mockResolvedValue({
    mode: 'LIVE',
    active: true,
    expires_at: '2026-09-07T13:00:00+00:00',
    remaining_seconds: 1800,
    listing_ids: ['inv-1'],
    started_by_user_id: 'user-1',
    deployment_writes_enabled: true,
    deployment_listing_ids: ['inv-1'],
    max_session_minutes: 30,
    ...over,
  } as never)
}

/** The current deployment: writes off, no session. */
function safeMode() {
  vi.mocked(api.getPricingSession).mockResolvedValue({
    mode: 'SAFE',
    active: false,
    expires_at: null,
    remaining_seconds: 0,
    listing_ids: [],
    started_by_user_id: null,
    deployment_writes_enabled: false,
    deployment_listing_ids: [],
    max_session_minutes: 30,
  } as never)
}

/**
 * The reported V1 blocker: an owner opens /vacancy with pricing writes off,
 * clicks Review, and gets a red "Failed. ENABLE_PRICING_WRITES is not enabled".
 *
 * The backend was right the whole time — nothing reached PriceLabs. The bug
 * was that **Review was not review**: it called `submitPricingAction`, which
 * parked a real DANGEROUS approval that could never succeed, so the only way
 * forward was a failure box for a safety switch that was simply off.
 *
 * These tests hold the fix from the owner's side. The strongest of them assert
 * what is *not* called: Review issues no request at all, so it cannot create a
 * run, an approval, an audit event or a cleanup row — not by being careful,
 * but by calling nothing.
 */

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
    plain_reason: 'Your price is above what this property converts at.',
    plain_action: 'Lower this date',
    fingerprint: 'abc123',
    actionable: true,
    blocked_reason: null,
    pricelabs_minimum: 143,
    hard_floor: 143,
    normal_floor: 170,
    owner_floor: 175,
    owner_floor_basis: 'flat at the hard floor',
    auto_raise_ceiling: 252,
    absolute_ceiling: 324,
    market_p25: 240,
    market_booked_median: 300,
    market_occupancy: 50,
    listing_occupancy: 70,
    demand: 'Low Demand',
    pickup_7_days: 9,
    pinned_price: null,
    events: null,
    market_signal_conflict: false,
    historical_lead_band_adr: 160.5,
    history_sample_count: 7,
    historical_reference_gap_dollars: 39.5,
    historical_reference_gap_pct: 19.8,
    below_owner_floor: false,
    booking_com_warning: null,
    observed_commission_rate: null,
    stale: false,
    last_refreshed_at: '2026-09-07T08:59:52+00:00',
    ...over,
  }
}

function page(
  over: Partial<PricingRecommendationPage> = {},
): PricingRecommendationPage {
  return {
    generated_at: '2026-09-07T12:00:00+00:00',
    horizon_days: 60,
    // The reported condition: the deployment kill switch is off.
    writes_enabled: false,
    unblocked_actions: [],
    max_change_per_run: 0.1,
    recommendations: [rec()],
    bands: [],
    ...over,
  }
}

function serve(body: PricingRecommendationPage) {
  vi.mocked(api.getPricingRecommendations).mockResolvedValue(body)
}

async function review() {
  const user = userEvent.setup()

  await user.click(await screen.findByRole('button', { name: 'Review' }))

  return user
}

beforeEach(() => {
  vi.resetAllMocks()
  safeMode()
  safeMode()
})

describe('Review, with pricing writes disabled', () => {
  it('creates nothing at all — it issues no request', async () => {
    serve(page())

    renderWithRouter(<RecommendedActions />)
    await review()

    // The whole guarantee. No submit means no run, no approval, no audit
    // event and no cleanup row, because none of them can be created without
    // one of these calls.
    expect(api.submitPricingAction).not.toHaveBeenCalled()
    expect(api.resolveApproval).not.toHaveBeenCalled()

    // The page fetched its data once, on mount, and Review added nothing.
    expect(api.getPricingRecommendations).toHaveBeenCalledTimes(1)
  })

  it('shows no red failure box for a switch that is merely off', async () => {
    serve(page())

    renderWithRouter(<RecommendedActions />)
    await review()

    expect(screen.queryByText(/^Failed\./)).not.toBeInTheDocument()
    expect(screen.queryByText(/ENABLE_PRICING_WRITES/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says live pricing is off, and offers no approval path', async () => {
    serve(page())

    renderWithRouter(<RecommendedActions />)
    await review()

    expect(screen.getByText('LIVE PRICING IS OFF')).toBeInTheDocument()
    expect(
      screen.getByText('Review only — nothing can be sent to PriceLabs.'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Start a live pricing session to request approval.'),
    ).toBeInTheDocument()

    expect(screen.getByRole('button', { name: 'Request approval' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
  })

  it('still shows the complete evidence', async () => {
    serve(page())

    renderWithRouter(<RecommendedActions />)
    await review()

    for (const label of [
      'Days out',
      'Current price',
      'Proposed price',
      'Market p25',
      'Market booked median',
      'Market occupancy',
      'Listing occupancy',
      'Demand',
      'Hard floor',
      'Owner floor tonight',
      'Confidence',
      'Historical lead-band ADR',
      'Gap to that history',
      'Observed Booking.com commission',
      'PriceLabs refreshed',
      'State fingerprint',
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }

    expect(screen.getByText('$161 (n=7)')).toBeInTheDocument()
    expect(screen.getByText('near-term vacancy')).toBeInTheDocument()
    expect(screen.getByText('2026-09-07T08:59:52+00:00')).toBeInTheDocument()
  })

  it('can be closed again without touching anything', async () => {
    serve(page())

    renderWithRouter(<RecommendedActions />)
    const user = await review()

    await user.click(screen.getByRole('button', { name: 'Close' }))

    expect(screen.queryByText('State fingerprint')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument()
    expect(api.submitPricingAction).not.toHaveBeenCalled()
  })
})

describe('Review renders each action type', () => {
  it('reviews a LOWER', async () => {
    serve(page())

    renderWithRouter(<RecommendedActions />)
    await review()

    // $200 appears on the card face and again in the evidence panel; the
    // proposed price is the one unique to the review.
    expect(screen.getAllByText('$200').length).toBeGreaterThan(0)
    expect(screen.getByText('$190')).toBeInTheDocument()
  })

  it('reviews a RAISE', async () => {
    serve(
      page({
        recommendations: [
          rec({ action: 'RAISE', proposed_price: 220, pct_change: 10 }),
        ],
      }),
    )

    renderWithRouter(<RecommendedActions />)
    await review()

    expect(screen.getByText('$220')).toBeInTheDocument()
    expect(api.submitPricingAction).not.toHaveBeenCalled()
  })

  it('reviews a REMOVE_PIN and explains what removal actually does', async () => {
    serve(
      page({
        recommendations: [
          rec({ action: 'REMOVE_PIN', proposed_price: null, pct_change: null }),
        ],
      }),
    )

    renderWithRouter(<RecommendedActions />)
    await review()

    // It must promise a return of control, never a better price.
    expect(
      screen.getByText(/returns pricing control for 2026-09-20 to PriceLabs/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/does not guarantee a higher price, more revenue, or a booking/),
    ).toBeInTheDocument()
    expect(screen.getByText('back to dynamic')).toBeInTheDocument()
  })
})

describe('Review surfaces the flags that need a human', () => {
  it('shows a market-signal conflict', async () => {
    serve(page({ recommendations: [rec({ market_signal_conflict: true })] }))

    renderWithRouter(<RecommendedActions />)
    await review()

    expect(screen.getByText(/Market-signal conflict/)).toBeInTheDocument()
  })

  it('shows the Booking.com warning where one applies', async () => {
    serve(
      page({
        recommendations: [
          rec({
            booking_com_warning:
              'Booking.com exposure is not fully verified. Lowering the published price may result in an even lower guest-facing rate.',
            observed_commission_rate: 23,
          }),
        ],
      }),
    )

    renderWithRouter(<RecommendedActions />)
    await review()

    expect(
      screen.getByText(/Booking.com exposure is not fully verified/),
    ).toBeInTheDocument()
    expect(screen.getByText('23%')).toBeInTheDocument()
  })

  it('names an owner-floor exception rather than burying it', async () => {
    serve(page({ recommendations: [rec({ below_owner_floor: true })] }))

    renderWithRouter(<RecommendedActions />)
    await review()

    expect(
      screen.getByText(/below the owner floor.*explicit vacancy decision/s),
    ).toBeInTheDocument()
  })
})

describe('a refusal is not a failure', () => {
  /**
   * `_refused()` returns `outcome: CONFIRMED_FAILED` **and** a `refusal` code.
   * The console used to read only the first, so "the switch was off, nothing
   * was attempted" and "PriceLabs rejected the write" rendered identically, in
   * red. They are different facts and now look different.
   */
  async function applyWith(outcome: Record<string, unknown>) {
    // The write path needs all three layers agreeing, so this block opts into
    // a live deployment and a session covering the fixture listing.
    liveSession()
    serve(page({ writes_enabled: true, unblocked_actions: ['LOWER'] }))

    vi.mocked(api.submitPricingAction).mockResolvedValue({
      run_id: 'run-1',
      status: 'WAITING_FOR_APPROVAL',
      answer: '',
      trace: [],
      approval_required: {
        approval_id: 'ap-1',
        run_id: 'run-1',
        tool: 'apply_pricing_action',
        arguments: {},
        risk: 'DANGEROUS',
      },
    } as never)

    vi.mocked(api.resolveApproval).mockResolvedValue({
      approval_id: 'ap-1',
      approved: true,
      tool: 'apply_pricing_action',
      result: outcome,
      run_id: 'run-1',
      run_status: 'COMPLETED',
      answer: '',
      trace: [],
    } as never)

    renderWithRouter(<RecommendedActions />)

    const user = await review()

    await user.click(screen.getByRole('button', { name: 'Request approval' }))
    await user.click(await screen.findByRole('button', { name: /Approve & Apply/ }))
  }

  it('renders a refused action as not attempted, not as a failure', async () => {
    await applyWith({
      outcome: 'CONFIRMED_FAILED',
      refusal: 'WRITES_DISABLED',
      stay_date: '2026-09-20',
      message: 'ENABLE_PRICING_WRITES is not enabled; no price was changed.',
      needs_human: false,
    })

    expect(await screen.findByText('Not attempted.')).toBeInTheDocument()
    expect(screen.queryByText(/^Failed\./)).not.toBeInTheDocument()
  })

  it('still renders a genuine provider failure as a failure', async () => {
    await applyWith({
      outcome: 'CONFIRMED_FAILED',
      refusal: null,
      stay_date: '2026-09-20',
      message: 'PriceLabs rejected the override.',
      needs_human: false,
    })

    expect(await screen.findByText(/Failed\./)).toBeInTheDocument()
    expect(screen.queryByText('Not attempted.')).not.toBeInTheDocument()
  })
})

describe('all three layers must agree before a write path appears', () => {
  /**
   * The session narrows; it never widens. A card offers approval only when
   * the deployment kill switch, the deployment allowlist and an active
   * session covering that listing all agree. Anything less stays review-only,
   * because an approval that provably cannot execute is the bug this work
   * removed.
   */
  async function reviewWith(over: Record<string, unknown>) {
    vi.mocked(api.getPricingSession).mockResolvedValue({
      mode: 'LIVE',
      active: true,
      expires_at: '2026-09-07T13:00:00+00:00',
      remaining_seconds: 1800,
      listing_ids: ['inv-1'],
      started_by_user_id: 'user-1',
      deployment_writes_enabled: true,
      deployment_listing_ids: ['inv-1'],
      max_session_minutes: 30,
      ...over,
    } as never)

    serve(page({ writes_enabled: true, unblocked_actions: ['LOWER'] }))

    renderWithRouter(<RecommendedActions />)
    await review()
  }

  it('offers Request approval when every layer agrees', async () => {
    await reviewWith({})

    expect(screen.getByRole('button', { name: 'Request approval' })).toBeEnabled()
    expect(screen.queryByText('LIVE PRICING IS OFF')).not.toBeInTheDocument()
  })

  it('stays review-only when the session excludes this listing', async () => {
    await reviewWith({ listing_ids: ['some-other-listing'] })

    expect(screen.getByText('LIVE PRICING IS OFF')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Request approval' })).toBeDisabled()
  })

  it('stays review-only when the deployment kill switch is shut', async () => {
    await reviewWith({ deployment_writes_enabled: false })

    expect(screen.getByText('LIVE PRICING IS OFF')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Request approval' })).toBeDisabled()
  })

  it('stays review-only when no session is open', async () => {
    await reviewWith({ mode: 'SAFE', active: false, listing_ids: [] })

    expect(screen.getByText('LIVE PRICING IS OFF')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Request approval' })).toBeDisabled()
  })
})
