import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PricingSessionBadge, PricingSessionControl } from './PricingSessionControl'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type { PricingBandsOut, PricingSession } from '../api/types'

vi.mock('../api/agentguard')

/**
 * The session control, and the two rules it must not break.
 *
 * It must never offer a write path the deployment forbids — that is the
 * doomed-approval bug in a new costume — and it must not make selecting the
 * whole portfolio a single click.
 */

function band(over: Partial<PricingBandsOut> = {}): PricingBandsOut {
  return {
    listing_id: 'inv-1',
    slug: 'invented',
    display_name: 'Invented Cottage',
    hard_floor: 143,
    normal_floor: 170,
    auto_raise_ceiling: 252,
    absolute_ceiling: 324,
    automation_enabled: false,
    raise_requires_human: false,
    ...over,
  } as PricingBandsOut
}

const BANDS = [
  band(),
  band({ listing_id: 'inv-2', slug: 'second', display_name: 'Second Home' }),
]

function session(over: Partial<PricingSession> = {}): PricingSession {
  return {
    mode: 'SAFE',
    active: false,
    expires_at: null,
    remaining_seconds: 0,
    listing_ids: [],
    started_by_user_id: null,
    deployment_writes_enabled: false,
    deployment_listing_ids: [],
    max_session_minutes: 30,
    ...over,
  }
}

function serve(body: PricingSession) {
  vi.mocked(api.getPricingSession).mockResolvedValue(body)
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('the status badge', () => {
  it('says SAFE MODE when nothing is open', async () => {
    serve(session())

    renderWithRouter(<PricingSessionBadge />)

    expect(await screen.findByText('SAFE MODE')).toBeInTheDocument()
  })

  it('counts down and names the enabled listings when live', async () => {
    serve(
      session({
        mode: 'LIVE',
        active: true,
        remaining_seconds: 18 * 60,
        listing_ids: ['inv-1', 'inv-2'],
        deployment_writes_enabled: true,
      }),
    )

    renderWithRouter(<PricingSessionBadge />)

    expect(await screen.findByText('LIVE — 18 min')).toBeInTheDocument()
    expect(screen.getByText('inv-1, inv-2')).toBeInTheDocument()
  })
})

describe('starting a session', () => {
  it('warns that the deployment forbids pricing changes', async () => {
    serve(session())

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    expect(
      await screen.findByText('DEPLOYMENT PRICING IS DISABLED'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/no change can be applied even during a session/),
    ).toBeInTheDocument()
  })

  it('shows the confirmation before anything is opened', async () => {
    serve(session({ deployment_writes_enabled: true }))

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    const user = userEvent.setup()

    await user.click(
      await screen.findByRole('button', { name: 'Start pricing session' }),
    )

    expect(
      screen.getByText(
        /During this session, individually approved pricing changes can be sent to PriceLabs\. Every change still requires its own human approval\./,
      ),
    ).toBeInTheDocument()

    expect(api.startPricingSession).not.toHaveBeenCalled()
  })

  it('selects no listing by default and offers no select-all', async () => {
    serve(session({ deployment_writes_enabled: true }))

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    const user = userEvent.setup()

    await user.click(
      await screen.findByRole('button', { name: 'Start pricing session' }),
    )

    for (const box of screen.getAllByRole('checkbox')) {
      expect(box).not.toBeChecked()
    }

    expect(screen.getByText('Listings (0 selected)')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /select all/i })).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /all/i })).toBeNull()
  })

  it('cannot be submitted until at least one listing is chosen', async () => {
    serve(session({ deployment_writes_enabled: true }))

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    const user = userEvent.setup()

    await user.click(
      await screen.findByRole('button', { name: 'Start pricing session' }),
    )

    const submit = screen.getAllByRole('button', {
      name: 'Start pricing session',
    })[0]

    expect(submit).toBeDisabled()

    await user.click(screen.getAllByRole('checkbox')[0])

    expect(submit).toBeEnabled()
  })

  it('sends only the chosen listings and the duration', async () => {
    serve(session({ deployment_writes_enabled: true }))
    vi.mocked(api.startPricingSession).mockResolvedValue(
      session({ mode: 'LIVE', active: true }),
    )

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    const user = userEvent.setup()

    await user.click(
      await screen.findByRole('button', { name: 'Start pricing session' }),
    )
    await user.click(screen.getAllByRole('checkbox')[1])
    await user.click(
      screen.getAllByRole('button', { name: 'Start pricing session' })[0],
    )

    await waitFor(() => {
      expect(api.startPricingSession).toHaveBeenCalledWith(['inv-2'], 30)
    })
  })

  it('is unavailable to a non-administrator', async () => {
    serve(session({ deployment_writes_enabled: true }))

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={false} />)

    expect(
      await screen.findByText('Only an administrator can start a pricing session.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start pricing session' })).toBeNull()
  })
})

describe('an open session', () => {
  it('shows the remaining time and the enabled listings', async () => {
    serve(
      session({
        mode: 'LIVE',
        active: true,
        remaining_seconds: 18 * 60,
        listing_ids: ['inv-1'],
        deployment_writes_enabled: true,
      }),
    )

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    expect(await screen.findByText('LIVE PRICING ON')).toBeInTheDocument()
    expect(
      screen.getByText(/18 minutes remaining · Enabled listings: inv-1/),
    ).toBeInTheDocument()
  })

  it('can be ended immediately', async () => {
    serve(
      session({
        mode: 'LIVE',
        active: true,
        remaining_seconds: 600,
        listing_ids: ['inv-1'],
        deployment_writes_enabled: true,
      }),
    )
    vi.mocked(api.endPricingSession).mockResolvedValue(session())

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: 'End pricing session' }))

    await waitFor(() => {
      expect(api.endPricingSession).toHaveBeenCalled()
    })
  })

  it('reads as SAFE once it has expired', async () => {
    // The server reports the lapsed session as SAFE; the console has no
    // separate expiry logic to disagree with it.
    serve(session({ mode: 'SAFE', active: false, remaining_seconds: 0 }))

    renderWithRouter(<PricingSessionControl bands={BANDS} canAdminister={true} />)

    expect(await screen.findByText('LIVE PRICING: OFF')).toBeInTheDocument()
    expect(screen.queryByText('LIVE PRICING ON')).toBeNull()
  })
})
