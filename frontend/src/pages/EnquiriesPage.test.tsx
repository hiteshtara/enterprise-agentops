import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { EnquiriesPage } from './EnquiriesPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type {
  AgentResponse,
  ApprovalRequest,
  ApprovalResponse,
  EnquiryPage,
  EnquiryReplyDraft,
  EnquirySendOutcome,
  EnquirySummary,
} from '../api/types'

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
  detail: 'Review and edit it. Nothing is sent until a human approves the send.',
}

const APPROVAL_ID = 'ap-enquiry-0001'

const RUN_ID = 'run-enquiry-0001'

const EDITED = 'I will confirm those dates today and come back with the total.'

const approval: ApprovalRequest = {
  approval_id: APPROVAL_ID,
  run_id: RUN_ID,
  requested_by_user_id: 'us-operator',
  tool: 'send_enquiry_reply',
  arguments: {
    enquiry_ref: ENQUIRY.enquiry_ref,
    subject: 'Re: your enquiry',
    message: EDITED,
  },
  risk: 'DANGEROUS',
}

const WAITING: AgentResponse = {
  run_id: RUN_ID,
  status: 'WAITING_FOR_APPROVAL',
  answer: 'Approval required before executing send_enquiry_reply.',
  trace: [],
  approval_required: approval,
}

function resolved(result: EnquirySendOutcome | null): ApprovalResponse {
  return {
    approval_id: APPROVAL_ID,
    approved: result !== null,
    tool: 'send_enquiry_reply',
    result,
    run_id: RUN_ID,
    run_status: result === null ? 'CANCELLED' : 'COMPLETED',
    answer: 'The approved action was executed.',
    trace: [],
    approval_required: null,
  }
}

const CONFIRMED_SENT: EnquirySendOutcome = {
  status: 'confirmed_sent',
  enquiry_ref: ENQUIRY.enquiry_ref,
  message: 'Lodgify reports the message as Sent.',
  messages: [
    {
      message_ref: 'm-eeee',
      message_status: 'Sent',
      created_at: '2026-09-03T10:00:00',
    },
  ],
}

const CONFIRMED_FAILED: EnquirySendOutcome = {
  status: 'confirmed_failed',
  enquiry_ref: ENQUIRY.enquiry_ref,
  message: 'Nothing was sent. The provider rejected the message (400).',
  messages: [],
}

const UNKNOWN: EnquirySendOutcome = {
  status: 'unknown_send_state',
  enquiry_ref: ENQUIRY.enquiry_ref,
  message:
    'Delivery could not be confirmed. Do not resend automatically. Check the Lodgify thread before taking further action.',
  messages: [],
}

async function openCard() {
  return await screen.findByRole('article', { name: 'Renovated 2nd-Floor Home' })
}

async function generate() {
  const card = await openCard()

  await userEvent.click(within(card).getByRole('button', { name: 'Generate reply' }))

  await within(card).findByRole('textbox', { name: 'Message' })

  return card
}

async function submit(card: HTMLElement) {
  await userEvent.click(within(card).getByRole('button', { name: 'Send for approval' }))

  return await within(card).findByRole('region', { name: 'Approval required' })
}

describe('EnquiriesPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.listEnquiries).mockResolvedValue(PAGE)
    vi.mocked(api.generateEnquiryReply).mockResolvedValue(DRAFT)
    vi.mocked(api.requestEnquiryReply).mockResolvedValue(WAITING)
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

  it('offers no send control until a draft has been generated', async () => {
    renderWithRouter(<EnquiriesPage />)

    await openCard()

    for (const control of screen.getAllByRole('button')) {
      expect(control.textContent ?? '').not.toMatch(/send|approve|reject|queue/i)
    }

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('says plainly that nothing reaches anyone without an approval', async () => {
    renderWithRouter(<EnquiriesPage />)

    await screen.findByText('Renovated 2nd-Floor Home')

    expect(
      screen.getByText(
        /Nothing reaches the person who enquired until you send it for approval and a human approves it/,
      ),
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
  // -- generate, edit, approve, send ---------------------------------------

  it("submits the operator's edited text, and sends nothing itself", async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await generate()
    const box = within(card).getByRole('textbox', { name: 'Message' })

    await userEvent.clear(box)
    await userEvent.type(box, EDITED)
    await submit(card)

    expect(api.requestEnquiryReply).toHaveBeenCalledWith(
      ENQUIRY.enquiry_ref,
      'Re: your enquiry',
      EDITED,
    )

    // The console never executes the tool: submitting only parks a run.
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('seeds the editor with the generated draft', async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await generate()

    expect(within(card).getByRole('textbox', { name: 'Message' })).toHaveValue(
      DRAFT.message,
    )
    expect(within(card).getByRole('textbox', { name: 'Subject' })).toHaveValue(
      DRAFT.subject,
    )
  })

  it('shows the exact outgoing message in the approval card', async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await generate()
    const region = await submit(card)

    expect(within(region).getByText(EDITED)).toBeInTheDocument()
    expect(within(region).getByText('Send enquiry reply')).toBeInTheDocument()
    expect(within(region).getByText(ENQUIRY.enquiry_ref)).toBeInTheDocument()
  })

  it('reports a confirmed send and marks the enquiry sent', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue(resolved(CONFIRMED_SENT))

    renderWithRouter(<EnquiriesPage />)

    const card = await generate()

    await submit(card)
    await userEvent.click(within(card).getByRole('button', { name: 'Approve & Send' }))

    await waitFor(() =>
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, true),
    )

    expect(await within(card).findByText(CONFIRMED_SENT.message)).toBeInTheDocument()
    expect(within(card).getByText('Sent')).toBeInTheDocument()

    // Nothing offers to send again.
    expect(
      within(card).queryByRole('button', { name: 'Send for approval' }),
    ).not.toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Generate reply' })).toBeDisabled()
  })

  it('does not mark the enquiry sent when the send confirmably failed', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue(resolved(CONFIRMED_FAILED))

    renderWithRouter(<EnquiriesPage />)

    const card = await generate()

    await submit(card)
    await userEvent.click(within(card).getByRole('button', { name: 'Approve & Send' }))

    expect(await within(card).findByText(CONFIRMED_FAILED.message)).toBeInTheDocument()
    expect(within(card).queryByText('Sent')).not.toBeInTheDocument()
    expect(within(card).getByText('Awaiting reply')).toBeInTheDocument()

    // Nothing was sent, so composing again is not a duplicate.
    expect(
      within(card).getByRole('button', { name: 'Send for approval' }),
    ).toBeInTheDocument()
  })

  it('asks for a person after an uncertain send, and offers no retry', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue(resolved(UNKNOWN))

    renderWithRouter(<EnquiriesPage />)

    const card = await generate()

    await submit(card)
    await userEvent.click(within(card).getByRole('button', { name: 'Approve & Send' }))

    expect(await within(card).findByText(UNKNOWN.message)).toBeInTheDocument()
    expect(within(card).getByText(/Needs a person/)).toBeInTheDocument()

    // The message may already have arrived. Nothing here may send a second one.
    expect(
      within(card).queryByRole('button', { name: 'Send for approval' }),
    ).not.toBeInTheDocument()
    expect(within(card).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Generate reply' })).toBeDisabled()
    expect(api.resolveApproval).toHaveBeenCalledTimes(1)
  })

  it('sends nothing when the approval is rejected', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue(resolved(null))

    renderWithRouter(<EnquiriesPage />)

    const card = await generate()

    await submit(card)
    await userEvent.click(within(card).getByRole('button', { name: 'Reject' }))

    await waitFor(() =>
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, false),
    )

    expect(within(card).queryByText('Sent')).not.toBeInTheDocument()
    expect(await within(card).findByRole('textbox', { name: 'Message' })).toHaveValue(
      DRAFT.message,
    )
  })

  it('cannot submit an empty reply', async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await generate()

    await userEvent.clear(within(card).getByRole('textbox', { name: 'Message' }))

    expect(
      within(card).getByRole('button', { name: 'Send for approval' }),
    ).toBeDisabled()
    expect(api.requestEnquiryReply).not.toHaveBeenCalled()
  })

  it('submits for one enquiry only', async () => {
    renderWithRouter(<EnquiriesPage />)

    const card = await generate()

    await submit(card)

    const other = screen.getByRole('article', { name: 'Boston condo second Floor' })

    expect(
      within(other).queryByRole('region', { name: 'Approval required' }),
    ).not.toBeInTheDocument()
    expect(within(other).queryByRole('textbox')).not.toBeInTheDocument()
  })
})
