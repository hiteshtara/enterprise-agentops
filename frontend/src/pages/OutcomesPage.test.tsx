import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { OutcomesPage } from './OutcomesPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type {
  PricingActionOutcome,
  PricingActionOutcomePage,
  ReconcilerHealth,
} from '../api/types'

vi.mock('../api/agentguard')

const DISCLAIMER =
  'This records what happened after a pricing action, not because of one.'

function outcome(over: Partial<PricingActionOutcome> = {}): PricingActionOutcome {
  return {
    id: 'o-1',
    approval_id: 'ap-1',
    run_id: 'run-1',
    cleanup_id: 'cl-1',
    listing_id: 'inv-1',
    stay_date: '2026-09-10',
    action: 'LOWER',
    executed_at: '2026-09-06T10:00:00+00:00',
    write_outcome: 'CONFIRMED_APPLIED',
    price_before: 182,
    price_after: 164,
    currency: 'USD',
    days_out: 4,
    history_adr: 149.5,
    history_sample_count: 9,
    market_p25: 199.8,
    market_booked_median: 233,
    demand: 'Normal Demand',
    listing_occupancy: 65,
    market_occupancy: 46.09,
    market_signal_conflict: true,
    hard_floor: 143,
    owner_floor: 143,
    observed_commission_rate: 23,
    first_booked_at: null,
    first_booking_lead_days: null,
    first_hours_from_action: null,
    first_realized_stay_adr: null,
    first_booking_channel: null,
    current_booking_status: null,
    current_realized_stay_adr: null,
    current_booking_channel: null,
    cancelled_after_booking: null,
    last_reconciled_at: null,
    reconcile_pass_count: 0,
    cancellation_first_observed_at: null,
    finalized_at: null,
    reopened_at: null,
    ...over,
  }
}

function page(over: Partial<PricingActionOutcomePage> = {}): PricingActionOutcomePage {
  return {
    outcomes: [],
    executed: 0,
    booked_after_action: 0,
    booked_then_cancelled: 0,
    never_booked: 0,
    unknown: 0,
    disclaimer: DISCLAIMER,
    ...over,
  }
}

function health(over: Partial<ReconcilerHealth> = {}): ReconcilerHealth {
  return {
    checked_at: '2026-09-07T00:00:00+00:00',
    outcomes: 1,
    unreconciled: 0,
    oldest_unreconciled_executed_at: null,
    oldest_unreconciled_age_hours: null,
    last_successful_reconciliation_at: '2026-09-07T00:00:00+00:00',
    finalized: 0,
    awaiting_first_booking: 1,
    booked_currently: 0,
    cancelled_after_booking: 0,
    reopened: 0,
    ...over,
  }
}

function serve(body: PricingActionOutcomePage, status = health()) {
  vi.mocked(api.getPricingOutcomes).mockResolvedValue(body)
  vi.mocked(api.getReconcilerHealth).mockResolvedValue(status)
}

async function row() {
  const table = await screen.findByRole('table')

  return within(within(table).getAllByRole('rowgroup')[1]).getAllByRole('row')[0]
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('OutcomesPage', () => {
  it('states that it is not a causal claim, using the API wording', async () => {
    serve(page({ outcomes: [outcome()], executed: 1, unknown: 1 }))

    renderWithRouter(<OutcomesPage />)

    // Taken from the payload, not written in the component, so the API and
    // the console can never say different things about causation.
    expect(await screen.findByText(DISCLAIMER)).toBeInTheDocument()
  })

  it('makes no revenue, uplift or attribution claim anywhere', async () => {
    serve(
      page({
        outcomes: [
          outcome({
            current_booking_status: 'booked',
            first_booked_at: '2026-09-06T16:30:00.000Z',
            first_hours_from_action: 6.5,
            first_realized_stay_adr: 170,
          }),
        ],
        executed: 1,
        booked_after_action: 1,
      }),
    )

    renderWithRouter(<OutcomesPage />)

    await screen.findByRole('table')

    const text = document.body.textContent?.toLowerCase() ?? ''

    for (const forbidden of [
      'uplift',
      'attributed',
      'incremental',
      'roi',
      'caused',
      'revenue impact',
    ]) {
      expect(text).not.toContain(forbidden)
    }
  })

  it('shows an unreconciled row as unknown, never as "did not book"', async () => {
    serve(page({ outcomes: [outcome()], executed: 1, unknown: 1 }))

    renderWithRouter(<OutcomesPage />)

    const first = await row()

    expect(within(first).getByText('Not yet reconciled')).toBeInTheDocument()
    expect(within(first).queryByText(/never booked/i)).not.toBeInTheDocument()
    expect(within(first).queryByText('$0')).not.toBeInTheDocument()
  })

  it('reports the gap to a booking in hours, not rounded to days', async () => {
    serve(
      page({
        outcomes: [
          outcome({
            current_booking_status: 'booked',
            first_booked_at: '2026-09-06T16:30:00.000Z',
            first_hours_from_action: 6.5,
            first_booking_lead_days: 4,
            first_booking_channel: 'bcom',
          }),
        ],
        executed: 1,
        booked_after_action: 1,
      }),
    )

    renderWithRouter(<OutcomesPage />)

    const first = await row()

    expect(within(first).getByText('6.5h')).toBeInTheDocument()
    expect(within(first).queryByText('0d')).not.toBeInTheDocument()
  })

  it('distinguishes booked-then-cancelled from never booked', async () => {
    serve(
      page({
        outcomes: [
          outcome({
            id: 'o-cancelled',
            current_booking_status: 'cancelled',
            first_booked_at: '2026-09-06T16:30:00.000Z',
            cancelled_after_booking: true,
          }),
          outcome({
            id: 'o-empty',
            stay_date: '2026-09-11',
            current_booking_status: 'none',
          }),
        ],
        executed: 2,
        booked_then_cancelled: 1,
        never_booked: 1,
      }),
    )

    renderWithRouter(<OutcomesPage />)

    const table = await screen.findByRole('table')
    const body = within(table).getAllByRole('rowgroup')[1]

    // Scoped to the table: "Never booked" is also a summary tile label, and
    // the point here is the per-row badge.
    expect(
      within(body).getByText('Booked after the action, later cancelled'),
    ).toBeInTheDocument()
    expect(within(body).getByText('Never booked')).toBeInTheDocument()
  })

  it('labels realized ADR as a stay average, not this night rate', async () => {
    serve(page({ outcomes: [outcome()], executed: 1 }))

    renderWithRouter(<OutcomesPage />)

    await screen.findByRole('table')

    expect(screen.getByText(/stay average, not this night/i)).toBeInTheDocument()
  })

  it('warns when reconciliation is behind rather than showing zeros', async () => {
    serve(
      page({ outcomes: [outcome()], executed: 1, unknown: 1 }),
      health({
        unreconciled: 1,
        oldest_unreconciled_age_hours: 96,
        last_successful_reconciliation_at: null,
      }),
    )

    renderWithRouter(<OutcomesPage />)

    expect(await screen.findByText(/Reconciliation is behind/)).toBeInTheDocument()
    expect(screen.getByText(/not that the night went unsold/)).toBeInTheDocument()
  })

  it('treats an empty board as a legitimate state', async () => {
    serve(page())

    renderWithRouter(<OutcomesPage />)

    expect(
      await screen.findByText(/An empty board is a legitimate state/),
    ).toBeInTheDocument()
  })

  it('never renders a reservation id, because the API never sends one', async () => {
    serve(page({ outcomes: [outcome()], executed: 1 }))

    renderWithRouter(<OutcomesPage />)

    await screen.findByRole('table')

    expect(document.body.textContent).not.toContain('reservation_id')
  })

  it('imports no write path: the page cannot change a price', () => {
    // The board is decision *review*, not decision making. Approving or
    // changing a price still goes through /vacancy and an approval.
    expect(OutcomesPage.toString()).not.toContain('submitPricingAction')
    expect(OutcomesPage.toString()).not.toContain('resolveApproval')
  })
})
