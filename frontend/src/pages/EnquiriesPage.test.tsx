import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { EnquiriesPage } from './EnquiriesPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type { EnquiryPage, EnquiryReplyDraft, EnquirySummary } from '../api/types'

vi.mock('../api/agentguard')

const ENQUIRY: EnquirySummary = {
  enquiry_ref: 'EQ-ABCD2345',
  property_slug: 'renovated-2nd-floor-home',
  property_name: 'Renovated 2nd-Floor Home',
  source: 'Airbnb',
  arrival: '2026-12-04',
  departure: '2026-12-08',
  is_replied: false,
}

const SECOND: EnquirySummary = {
  ...ENQUIRY,
  enquiry_ref: 'EQ-EFGH6789',
  property_name: 'Boston condo second Floor',
  is_replied: true,
}

const PAGE: EnquiryPage = { enquiries: [ENQUIRY, SECOND], count: 2, total: 2 }

const DRAFT: EnquiryReplyDraft = {
  enquiry_ref: ENQUIRY.enquiry_ref,
  subject: 'Re: your enquiry',
  message: 'Thank you for your enquiry. Those dates are free — shall I hold them?',
  detail: 'Copy this into Lodgify yourself; AgentGuard will not send it.',
}

describe('EnquiriesPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.listEnquiries).mockResolvedValue(PAGE)
    vi.mocked(api.generateEnquiryReply).mockResolvedValue(DRAFT)
  })

  it('lists the open enquiries', async () => {
    renderWithRouter(<EnquiriesPage />)

    expect(await screen.findByText('Renovated 2nd-Floor Home')).toBeInTheDocument()
    expect(screen.getByText('Boston condo second Floor')).toBeInTheDocument()
    expect(screen.getByText('EQ-ABCD2345')).toBeInTheDocument()
    expect(screen.getAllByText(/2026-12-04/)).toHaveLength(2)
  })

  it('shows the returned text when Generate reply is pressed', async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await screen.findByRole('article', {
      name: 'Renovated 2nd-Floor Home',
    })

    await userEvent.click(within(card).getByRole('button', { name: 'Generate reply' }))

    expect(await within(card).findByText(DRAFT.message!)).toBeInTheDocument()
    expect(within(card).getByText(DRAFT.detail)).toBeInTheDocument()
    expect(api.generateEnquiryReply).toHaveBeenCalledWith('EQ-ABCD2345')
  })

  it('generates for one enquiry only', async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await screen.findByRole('article', {
      name: 'Renovated 2nd-Floor Home',
    })

    await userEvent.click(within(card).getByRole('button', { name: 'Generate reply' }))

    const other = screen.getByRole('article', { name: 'Boston condo second Floor' })

    expect(within(other).queryByText(DRAFT.message!)).not.toBeInTheDocument()
  })

  it('shows the backend reason when no draft could be produced', async () => {
    vi.mocked(api.generateEnquiryReply).mockResolvedValue({
      enquiry_ref: ENQUIRY.enquiry_ref,
      subject: null,
      message: null,
      detail: 'This enquiry could not be read, so nothing was drafted.',
    })

    renderWithRouter(<EnquiriesPage />)

    const card = await screen.findByRole('article', {
      name: 'Renovated 2nd-Floor Home',
    })

    await userEvent.click(within(card).getByRole('button', { name: 'Generate reply' }))

    expect(
      await within(card).findByText(
        'This enquiry could not be read, so nothing was drafted.',
      ),
    ).toBeInTheDocument()
  })

  it('has no send control anywhere on the page', async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await screen.findByRole('article', {
      name: 'Renovated 2nd-Floor Home',
    })

    await userEvent.click(within(card).getByRole('button', { name: 'Generate reply' }))

    await within(card).findByText(DRAFT.message!)

    // Structural, not cosmetic: nothing on this surface may send, approve or
    // queue a message, and there is no backend route that would let it.
    for (const control of screen.getAllByRole('button')) {
      expect(control.textContent ?? '').not.toMatch(/send|approve|reject|submit|queue/i)
    }

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('says plainly that AgentGuard will not send the draft', async () => {
    renderWithRouter(<EnquiriesPage />)

    await screen.findByText('Renovated 2nd-Floor Home')

    expect(
      screen.getByText(/AgentGuard will not send it or save it as an enquiry reply/),
    ).toBeInTheDocument()
  })

  it('does not claim that nothing at all is stored', async () => {
    // Drafting runs through the ordinary agent loop, so the prompt is
    // persisted as a Run like every other. The banner may promise that no
    // enquiry reply is saved and that nothing is sent; it may not promise the
    // database is untouched, because that is not true.
    renderWithRouter(<EnquiriesPage />)

    await screen.findByText('Renovated 2nd-Floor Home')

    expect(screen.queryByText(/nothing here is saved/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/nothing is stored/i)).not.toBeInTheDocument()
  })

  it('says how many of the open enquiries are being shown', async () => {
    vi.mocked(api.listEnquiries).mockResolvedValue({
      enquiries: [ENQUIRY, SECOND],
      count: 2,
      total: 47,
    })

    renderWithRouter(<EnquiriesPage />)

    // The number that stops twenty of forty-seven from reading as the queue.
    expect(
      await screen.findByText('Showing 2 of 47 open enquiries'),
    ).toBeInTheDocument()
  })

  it('does not say "of" when the whole queue is on screen', async () => {
    renderWithRouter(<EnquiriesPage />)

    expect(await screen.findByText('Showing 2 open enquiries')).toBeInTheDocument()
  })

  it('does not poll', async () => {
    renderWithRouter(<EnquiriesPage />)

    await screen.findByText('Renovated 2nd-Floor Home')

    expect(api.listEnquiries).toHaveBeenCalledTimes(1)
  })

  it('re-reads the list when Refresh is pressed', async () => {
    renderWithRouter(<EnquiriesPage />)

    await screen.findByText('Renovated 2nd-Floor Home')

    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(api.listEnquiries).toHaveBeenCalledTimes(2)
  })
})
