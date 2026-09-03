import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InboxPage } from './InboxPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { inboxPage } from '../test/factories'

vi.mock('../api/agentguard')

describe('InboxPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => vi.useRealTimers())

  it('lists conversations with property, channel and status', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('Needs attention')).toBeInTheDocument()
    expect(screen.getByText('Responded')).toBeInTheDocument()

    expect(screen.getByText('Renovated 2nd-Floor Home')).toBeInTheDocument()
    expect(screen.getByText('BookingCom')).toBeInTheDocument()

    expect(screen.getByText(/Is there parking/)).toBeInTheDocument()
  })

  it('never renders guest contact details', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    const { container } = renderWithRouter(<InboxPage />)

    await screen.findByText('Needs attention')

    expect(container.textContent).not.toMatch(/@/)
    expect(container.textContent).not.toMatch(/\+1555/)
  })

  it('links each row to its conversation by safe reference', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    const link = await screen.findByRole('link', { name: /Is there parking/ })

    expect(link).toHaveAttribute('href', '/inbox/PH-AAAAAAAA')
  })

  it('polls while the tab is visible', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    await screen.findByText('Needs attention')

    expect(api.getInbox).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(30_000)

    await waitFor(() => expect(api.getInbox).toHaveBeenCalledTimes(2))
  })

  it('pauses polling while the tab is hidden', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    await screen.findByText('Needs attention')

    const spy = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')

    await vi.advanceTimersByTimeAsync(90_000)

    expect(api.getInbox).toHaveBeenCalledTimes(1)

    spy.mockRestore()
  })

  it('refreshes on demand', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    await screen.findByText('Needs attention')

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })

    await user.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(api.getInbox).toHaveBeenCalledTimes(2))
  })

  it('shows an empty state when there is nothing to answer', async () => {
    vi.mocked(api.getInbox).mockResolvedValue({ conversations: [], count: 0 })

    renderWithRouter(<InboxPage />)

    expect(
      await screen.findByText('No recent guest conversations.'),
    ).toBeInTheDocument()
  })

  it('reports a load failure without leaking provider detail', async () => {
    vi.mocked(api.getInbox).mockRejectedValue(new Error('socket hang up'))

    renderWithRouter(<InboxPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Something went wrong loading this view.',
    )
  })
})
