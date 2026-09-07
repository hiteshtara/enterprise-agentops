import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CleanupPage } from './CleanupPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type { PricingCleanupRecord, PricingCleanupWorkload } from '../api/types'

vi.mock('../api/agentguard')

/**
 * The failure mode this page must not have: someone clicking a button they
 * believe removes an override, and being wrong. So the tests care as much
 * about wording and about what is *absent* as about behaviour.
 */

function record(over: Partial<PricingCleanupRecord> = {}): PricingCleanupRecord {
  return {
    id: 'cl-1',
    listing_id: 'inv-1',
    stay_date: '2026-09-08',
    old_price: 217,
    new_price: 196,
    currency: 'USD',
    state: 'NEEDS_REVIEW',
    adopted: false,
    approval_id: 'ap-1',
    run_id: 'run-1',
    marker: 'cl-1',
    reason_sent: 'AGENTGUARD:cl-1: 1d out and still open',
    provider_created_at: null,
    created_at: '2026-09-07T01:12:42+00:00',
    cleanup_at: '2026-09-06T00:00:00+00:00',
    resolved_at: '2026-09-07T01:12:44+00:00',
    resolution: 'the confirming re-read returned no creation time',
    manual_resolution: null,
    resolved_by_user_id: null,
    manually_resolved_at: null,
    ...over,
  }
}

function workload(over: Partial<PricingCleanupWorkload> = {}): PricingCleanupWorkload {
  return {
    counted_at: '2026-09-07T02:00:00+00:00',
    pending_write: 0,
    active: 35,
    due_now: 2,
    claimed: 0,
    delete_started: 0,
    needs_review: 1,
    unknown_cleanup_state: 0,
    oldest_overdue_at: '2026-09-05T00:00:00+00:00',
    oldest_overdue_hours: 50,
    needs_attention: [record()],
    ...over,
  }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.getCleanupWorkload).mockResolvedValue(workload())
})

describe('CleanupPage', () => {
  it('offers no way to delete, retry or force anything', async () => {
    renderWithRouter(<CleanupPage />)

    await screen.findByText(/Needs a person/)

    const labels = screen
      .getAllByRole('button')
      .map((b) => b.textContent?.toLowerCase() ?? '')

    for (const forbidden of ['delete', 'remove', 'retry', 'force', 'run', 'clean']) {
      expect(labels.join(' ')).not.toContain(forbidden)
    }

    // And the page imports no function that could reach a provider write.
    expect(CleanupPage.toString()).not.toContain('runPricingCleanup')
  })

  it('labels the only action unmistakably', async () => {
    renderWithRouter(<CleanupPage />)

    const button = await screen.findByRole('button', {
      name: 'Record manual resolution',
    })

    expect(button).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resolve' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cleanup' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('states that it does not change PriceLabs before submission', async () => {
    const user = userEvent.setup()

    renderWithRouter(<CleanupPage />)

    await user.click(
      await screen.findByRole('button', { name: 'Record manual resolution' }),
    )

    expect(
      screen.getByText(
        /This records something you already verified or performed manually\. It does not change PriceLabs\./,
      ),
    ).toBeInTheDocument()
  })

  it('shows what an investigator needs to find the override', async () => {
    renderWithRouter(<CleanupPage />)

    await screen.findByText(/Needs a person/)

    expect(screen.getByText('cl-1')).toBeInTheDocument()
    expect(
      screen.getByText('AGENTGUARD:cl-1: 1d out and still open'),
    ).toBeInTheDocument()
    expect(screen.getByText('never confirmed')).toBeInTheDocument()
  })

  it('records the resolution and refreshes, sending no actor', async () => {
    const user = userEvent.setup()

    vi.mocked(api.recordManualResolution).mockResolvedValue(
      record({ state: 'MANUALLY_RESOLVED' }),
    )

    renderWithRouter(<CleanupPage />)

    await user.click(
      await screen.findByRole('button', { name: 'Record manual resolution' }),
    )
    await user.type(
      screen.getByRole('textbox'),
      'Removed it in the PriceLabs UI and re-read to confirm.',
    )
    await user.click(screen.getByRole('button', { name: 'Record manual resolution' }))

    await waitFor(() => {
      expect(api.recordManualResolution).toHaveBeenCalledWith(
        'cl-1',
        'Removed it in the PriceLabs UI and re-read to confirm.',
      )
    })

    // Two arguments only: the row and the explanation. The actor comes from
    // the token, server-side.
    expect(vi.mocked(api.recordManualResolution).mock.calls[0]).toHaveLength(2)
  })

  it('will not submit an empty explanation', async () => {
    const user = userEvent.setup()

    renderWithRouter(<CleanupPage />)

    await user.click(
      await screen.findByRole('button', { name: 'Record manual resolution' }),
    )

    const submit = screen.getByRole('button', {
      name: 'Record manual resolution',
    })

    expect(submit).toBeDisabled()

    await user.type(screen.getByRole('textbox'), '   ')

    expect(submit).toBeDisabled()
    expect(api.recordManualResolution).not.toHaveBeenCalled()
  })

  it('offers no resolution action for a DELETE_STARTED row', async () => {
    vi.mocked(api.getCleanupWorkload).mockResolvedValue(
      workload({
        needs_review: 0,
        delete_started: 1,
        needs_attention: [record({ state: 'DELETE_STARTED' })],
      }),
    )

    renderWithRouter(<CleanupPage />)

    expect(await screen.findByText(/Treat it as hands off/)).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Record manual resolution' }),
    ).toBeNull()
  })

  it('says nothing is waiting when the queue is clear', async () => {
    vi.mocked(api.getCleanupWorkload).mockResolvedValue(
      workload({ needs_review: 0, needs_attention: [] }),
    )

    renderWithRouter(<CleanupPage />)

    expect(
      await screen.findByText(/Nothing is waiting on a person/),
    ).toBeInTheDocument()
  })

  it('warns when cleanup is behind', async () => {
    renderWithRouter(<CleanupPage />)

    expect(await screen.findByText(/Cleanup is behind/)).toBeInTheDocument()
    expect(screen.getByText(/This page never removes an override/)).toBeInTheDocument()
  })

  it('never renders a claim token or lease', async () => {
    renderWithRouter(<CleanupPage />)

    await screen.findByText(/Needs a person/)

    const text = document.body.textContent ?? ''

    expect(text).not.toContain('claim_token')
    expect(text).not.toContain('lease_until')
  })
})
