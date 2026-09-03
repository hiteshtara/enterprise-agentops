import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { ConversationPage } from './ConversationPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import type { CurrentUser } from '../api/types'
import {
  APPROVAL_ID,
  CONVERSATION_REF,
  GUEST_REPLY_BODY,
  GUEST_REPLY_SUBJECT,
  approverUser,
  confirmedFailed,
  confirmedSent,
  conversationDetail,
  guestReplyWaiting,
  sendResolved,
  unknownSendState,
} from '../test/factories'

vi.mock('../api/agentguard')

function renderConversation(user?: CurrentUser) {
  return renderWithRouter(
    <Routes>
      <Route path="/inbox/:conversationRef" element={<ConversationPage />} />
    </Routes>,
    { route: `/inbox/${CONVERSATION_REF}`, ...(user ? { user } : {}) },
  )
}

async function composeAndSubmit() {
  const user = userEvent.setup()

  await screen.findByText('Is there parking at the house?')

  await user.clear(screen.getByLabelText('Subject'))
  await user.type(screen.getByLabelText('Subject'), GUEST_REPLY_SUBJECT)
  await user.type(screen.getByLabelText('Message'), GUEST_REPLY_BODY)

  await user.click(screen.getByRole('button', { name: 'Send for approval' }))

  return user
}

describe('ConversationPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.getConversation).mockResolvedValue(conversationDetail)
  })

  it('renders the thread chronologically with guest and owner turns', async () => {
    renderConversation()

    const messages = await screen.findByTestId('messages')
    const entries = within(messages).getAllByText(/Guest|You/)

    expect(entries.map((node) => node.textContent)).toEqual(['Guest', 'You'])

    expect(
      within(messages).getByText('Is there parking at the house?'),
    ).toBeInTheDocument()
  })

  it('never renders guest contact details', async () => {
    const { container } = renderConversation()

    await screen.findByText('Is there parking at the house?')

    expect(container.textContent).not.toMatch(/@/)
  })

  it('lets the reply be edited before submission', async () => {
    renderConversation()

    await screen.findByText('Is there parking at the house?')

    const user = userEvent.setup()
    const body = screen.getByLabelText('Message')

    await user.type(body, 'Hello there')

    expect(body).toHaveValue('Hello there')

    // Nothing is submitted by typing.
    expect(api.requestGuestReply).not.toHaveBeenCalled()
  })

  it('submits the exact composed text for approval and sends nothing', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)

    renderConversation()

    await composeAndSubmit()

    await waitFor(() =>
      expect(api.requestGuestReply).toHaveBeenCalledWith(
        CONVERSATION_REF,
        GUEST_REPLY_SUBJECT,
        GUEST_REPLY_BODY,
      ),
    )

    // The console has no path that reaches the provider itself.
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('shows the exact outgoing message in the approval card, in full', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)

    renderConversation()

    await composeAndSubmit()

    const card = await screen.findByRole('region', { name: 'Approval required' })

    expect(within(card).getByText('Send guest message')).toBeInTheDocument()
    expect(within(card).getByText('DANGEROUS')).toBeInTheDocument()

    // The whole message, verbatim -- not truncated, not JSON-escaped.
    expect(within(card).getByText(GUEST_REPLY_BODY)).toBeInTheDocument()
    expect(within(card).getByText(GUEST_REPLY_SUBJECT)).toBeInTheDocument()

    expect(within(card).getByText('Renovated 2nd-Floor Home')).toBeInTheDocument()
    expect(within(card).getByText('BookingCom')).toBeInTheDocument()
  })

  it('reports a delivered send', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)
    vi.mocked(api.resolveApproval).mockResolvedValue(sendResolved(confirmedSent))

    renderConversation()

    const user = await composeAndSubmit()

    await user.click(await screen.findByRole('button', { name: 'Approve & Send' }))

    await waitFor(() =>
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, true),
    )

    expect(
      await screen.findByText('Lodgify reports the message as Delivered.'),
    ).toBeInTheDocument()
  })

  it('never claims an OTA channel on success', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)
    vi.mocked(api.resolveApproval).mockResolvedValue(sendResolved(confirmedSent))

    const { container } = renderConversation()

    const user = await composeAndSubmit()

    await user.click(await screen.findByRole('button', { name: 'Approve & Send' }))

    await screen.findByText('Lodgify reports the message as Delivered.')

    expect(container.textContent).not.toMatch(/Sent to (Airbnb|Vrbo|Booking)/i)
  })

  it('warns clearly on an unknown send state', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)
    vi.mocked(api.resolveApproval).mockResolvedValue(sendResolved(unknownSendState))

    renderConversation()

    const user = await composeAndSubmit()

    await user.click(await screen.findByRole('button', { name: 'Approve & Send' }))

    const notice = await screen.findByRole('status')

    expect(notice).toHaveTextContent('Delivery could not be confirmed.')
    expect(notice).toHaveTextContent('Do not resend automatically.')
  })

  it('reports an explicit failure as a failure', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)
    vi.mocked(api.resolveApproval).mockResolvedValue(sendResolved(confirmedFailed))

    renderConversation()

    const user = await composeAndSubmit()

    await user.click(await screen.findByRole('button', { name: 'Approve & Send' }))

    expect(await screen.findByText(/Nothing was sent/)).toBeInTheDocument()
  })

  it('rejecting sends nothing and clears the card', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)
    vi.mocked(api.resolveApproval).mockResolvedValue({
      ...sendResolved(confirmedSent),
      approved: false,
      result: null,
      run_status: 'CANCELLED',
    })

    renderConversation()

    const user = await composeAndSubmit()

    await user.click(await screen.findByRole('button', { name: 'Reject' }))

    await waitFor(() =>
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, false),
    )

    // Back to the compose form; no send result is claimed.
    expect(await screen.findByLabelText('Message')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('tells a non-admin their role cannot release the send', async () => {
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)

    renderConversation(approverUser)

    await composeAndSubmit()

    expect(
      await screen.findByText(/Your role cannot decide a DANGEROUS approval/),
    ).toBeInTheDocument()

    expect(
      screen.queryByRole('button', { name: 'Approve & Send' }),
    ).not.toBeInTheDocument()
  })

  it('drafts through the ordinary agent run rather than a hidden path', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'run-draft',
      status: 'COMPLETED',
      answer: 'Parking is shared and there is no extra charge.',
      trace: [],
      approval_required: null,
    })

    renderConversation()

    await screen.findByText('Is there parking at the house?')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Generate draft' }))

    await waitFor(() =>
      expect(screen.getByLabelText('Message')).toHaveValue(
        'Parking is shared and there is no extra charge.',
      ),
    )

    // A draft is model output only: nothing was submitted or sent.
    expect(api.requestGuestReply).not.toHaveBeenCalled()
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  describe('NO_REPLY_NEEDED', () => {
    function draftAnswering(answer: string) {
      vi.mocked(api.runAgent).mockResolvedValue({
        run_id: 'run-draft',
        status: 'COMPLETED',
        answer,
        trace: [],
        approval_required: null,
      })
    }

    async function generateDraft() {
      const user = userEvent.setup()

      await screen.findByText('Is there parking at the house?')
      await user.click(screen.getByRole('button', { name: 'Generate draft' }))

      return user
    }

    it('shows "No reply needed" instead of inventing text', async () => {
      draftAnswering('NO_REPLY_NEEDED')

      renderConversation()

      await generateDraft()

      expect(await screen.findByText('No reply needed')).toBeInTheDocument()

      // Nothing was written into the box for a person to accidentally send.
      expect(screen.getByLabelText('Message')).toHaveValue('')
    })

    it('sends nothing and creates no approval', async () => {
      draftAnswering('NO_REPLY_NEEDED')

      renderConversation()

      await generateDraft()

      await screen.findByText('No reply needed')

      expect(api.requestGuestReply).not.toHaveBeenCalled()
      expect(api.resolveApproval).not.toHaveBeenCalled()

      // And the action that would send stays unavailable while the box is empty.
      expect(screen.getByRole('button', { name: 'Send for approval' })).toBeDisabled()
    })

    it('tolerates a trailing full stop on the sentinel', async () => {
      draftAnswering('NO_REPLY_NEEDED.')

      renderConversation()

      await generateDraft()

      expect(await screen.findByText('No reply needed')).toBeInTheDocument()
    })

    it('treats a real draft as a draft, not as the sentinel', async () => {
      draftAnswering("You're very welcome!")

      renderConversation()

      await generateDraft()

      await waitFor(() =>
        expect(screen.getByLabelText('Message')).toHaveValue("You're very welcome!"),
      )

      expect(screen.queryByText('No reply needed')).not.toBeInTheDocument()
    })

    it('clears the notice once the host writes something', async () => {
      draftAnswering('NO_REPLY_NEEDED')

      renderConversation()

      const user = await generateDraft()

      await screen.findByText('No reply needed')

      await user.type(screen.getByLabelText('Message'), 'Actually, one thing…')

      expect(screen.queryByText('No reply needed')).not.toBeInTheDocument()
    })
  })
})

describe('historical retrieval indicator', () => {
  it('reports how many past replies informed the draft', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'run-draft',
      status: 'COMPLETED',
      answer: 'Parking is shared, no extra charge.',
      trace: [
        {
          tool: 'get_guest_conversation',
          arguments: { conversation_ref: CONVERSATION_REF },
          result: {
            historical_examples: {
              examples: [{ guest_example: 'a' }, { guest_example: 'b' }],
            },
          },
        },
      ],
      approval_required: null,
    })

    renderConversation()

    const user = userEvent.setup()

    await screen.findByText('Is there parking at the house?')
    await user.click(screen.getByRole('button', { name: 'Generate draft' }))

    expect(
      await screen.findByText(/Draft informed by 2 similar past replies/),
    ).toBeInTheDocument()
  })

  it('says nothing when no precedent was found', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'run-draft',
      status: 'COMPLETED',
      answer: 'Parking is shared.',
      trace: [],
      approval_required: null,
    })

    renderConversation()

    const user = userEvent.setup()

    await screen.findByText('Is there parking at the house?')
    await user.click(screen.getByRole('button', { name: 'Generate draft' }))

    await waitFor(() =>
      expect(screen.getByLabelText('Message')).toHaveValue('Parking is shared.'),
    )

    expect(screen.queryByText(/Draft informed by/)).not.toBeInTheDocument()
  })

  it('never renders the historical examples themselves', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      run_id: 'run-draft',
      status: 'COMPLETED',
      answer: 'Parking is shared.',
      trace: [
        {
          tool: 'get_guest_conversation',
          arguments: {},
          result: {
            historical_examples: {
              examples: [
                {
                  guest_example: 'SECRET PAST GUEST QUESTION',
                  owner_example: 'SECRET PAST OWNER REPLY',
                },
              ],
            },
          },
        },
      ],
      approval_required: null,
    })

    const { container } = renderConversation()

    const user = userEvent.setup()

    await screen.findByText('Is there parking at the house?')
    await user.click(screen.getByRole('button', { name: 'Generate draft' }))

    await screen.findByText(/Draft informed by 1 similar past reply/)

    // Another guest's conversation must not appear while writing to this one.
    expect(container.textContent).not.toContain('SECRET PAST GUEST QUESTION')
    expect(container.textContent).not.toContain('SECRET PAST OWNER REPLY')
  })
})
