import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InboxPage } from './InboxPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { inboxPage, reviewDraft, sentDraft, staleDraft } from '../test/factories'

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

  it('shows a neutral notice when a remembered conversation has no preview', async () => {
    vi.mocked(api.getInbox).mockResolvedValue({
      count: 1,
      conversations: [
        {
          ...inboxPage.conversations[0],
          conversation_ref: 'PH-HISTORIC1',
          last_message_excerpt: null,
          preview_unavailable: true,
        },
      ],
    })

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('Preview unavailable')).toBeInTheDocument()
    expect(screen.queryByText('No messages could be read.')).not.toBeInTheDocument()
  })

  it('still distinguishes an unreadable live thread', async () => {
    vi.mocked(api.getInbox).mockResolvedValue({
      count: 1,
      conversations: [
        {
          ...inboxPage.conversations[0],
          last_message_excerpt: null,
          preview_unavailable: false,
        },
      ],
    })

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('No messages could be read.')).toBeInTheDocument()
    expect(screen.queryByText('Preview unavailable')).not.toBeInTheDocument()
  })
})

describe('prepared replies in the Inbox', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => vi.useRealTimers())

  function withDraft(draft: (typeof inboxPage)['conversations'][number]['draft']) {
    vi.mocked(api.getInbox).mockResolvedValue({
      count: 1,
      conversations: [{ ...inboxPage.conversations[0], draft }],
    })
  }

  it('badges a conversation that already has a reply waiting', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('Draft ready')).toBeInTheDocument()
    expect(screen.getByText('No reply needed')).toBeInTheDocument()
  })

  it('previews the prepared reply so the owner can triage without opening it', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    expect(
      await screen.findByText(/Parking is shared out front, and there is no extra/),
    ).toBeInTheDocument()
  })

  it('marks a draft the conversation has moved past as stale', async () => {
    withDraft(staleDraft)

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('Draft stale')).toBeInTheDocument()

    // A stale draft is never previewed as if it were ready to send.
    expect(screen.queryByText(/Prepared reply/)).not.toBeInTheDocument()
  })

  it('surfaces a failed preparation rather than hiding it', async () => {
    withDraft(reviewDraft)

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('Needs human review')).toBeInTheDocument()
  })

  it('shows a sent conversation as sent', async () => {
    withDraft(sentDraft)

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('Sent')).toBeInTheDocument()
  })

  it('shows no draft badge when nothing has been prepared', async () => {
    withDraft(null)

    renderWithRouter(<InboxPage />)

    await screen.findByText('Needs attention')

    expect(screen.queryByText('Draft ready')).not.toBeInTheDocument()
    expect(screen.queryByText(/Prepared reply/)).not.toBeInTheDocument()
  })

  it('asks the backend to bring prepared replies up to date', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)

    renderWithRouter(<InboxPage />)

    // Polling is the recovery path for a webhook that never arrived, so it must
    // happen without the operator pressing anything.
    await waitFor(() => expect(api.refreshInbox).toHaveBeenCalled())
  })

  it('still lists conversations when preparation fails', async () => {
    vi.mocked(api.getInbox).mockResolvedValue(inboxPage)
    vi.mocked(api.refreshInbox).mockRejectedValue(new Error('model unavailable'))

    renderWithRouter(<InboxPage />)

    expect(await screen.findByText('Renovated 2nd-Floor Home')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
