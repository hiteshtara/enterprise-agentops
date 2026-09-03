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
  CONVERSATION_FINGERPRINT,
  CONVERSATION_REF,
  GUEST_REPLY_BODY,
  GUEST_REPLY_SUBJECT,
  approverUser,
  confirmedFailed,
  confirmedSent,
  conversationDetail,
  editedDraft,
  guestReplyWaiting,
  noReplyDraft,
  readyDraft,
  reviewDraft,
  sendResolved,
  staleDraft,
  staleReplyConflict,
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

  await user.clear(screen.getByLabelText('Message'))
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

    await user.clear(body)
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
        // The conversation state this text was written against. The server
        // refuses the submission if it has moved on since.
        CONVERSATION_FINGERPRINT,
      ),
    )

    // The console has no path that reaches the provider itself.
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('asks for a regenerate when the server refuses a submission as stale', async () => {
    vi.mocked(api.requestGuestReply).mockRejectedValue(staleReplyConflict)

    renderConversation()

    await composeAndSubmit()

    // The server is the authority on staleness, so its refusal is what the
    // operator is shown -- even though the console let this one through.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Regenerate the draft before sending/,
    )

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

  it('does not draft on demand -- the reply is already prepared', async () => {
    renderConversation()

    await screen.findByText('Is there parking at the house?')

    // Proactive drafting removes the "generate, then wait" step entirely.
    expect(screen.queryByRole('button', { name: 'Generate draft' })).toBeNull()
  })
})

describe('the prepared reply', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.getConversation).mockResolvedValue(conversationDetail)
  })

  function withDraft(draft: typeof readyDraft | null) {
    vi.mocked(api.getConversation).mockResolvedValue({
      ...conversationDetail,
      draft,
    })
  }

  it('is already in the editor when the conversation opens', async () => {
    renderConversation()

    await screen.findByText('Is there parking at the house?')

    expect(screen.getByLabelText('Message')).toHaveValue(readyDraft.message)
    expect(screen.getByLabelText('Subject')).toHaveValue(readyDraft.subject)

    // Opening a conversation reads it. It never prepares, and never sends.
    expect(api.regenerateDraft).not.toHaveBeenCalled()
    expect(api.requestGuestReply).not.toHaveBeenCalled()
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('shows the operator their own edited wording', async () => {
    withDraft(editedDraft)

    renderConversation()

    await screen.findByText('Is there parking at the house?')

    expect(screen.getByLabelText('Message')).toHaveValue(
      'My own wording for this guest.',
    )
  })

  it('persists an edit without sending anything', async () => {
    vi.mocked(api.editDraft).mockResolvedValue(editedDraft)

    renderConversation()

    await screen.findByText('Is there parking at the house?')

    const user = userEvent.setup()
    const body = screen.getByLabelText('Message')

    await user.clear(body)
    await user.type(body, 'My own wording for this guest.')
    await user.click(screen.getByRole('button', { name: 'Save edit' }))

    await waitFor(() =>
      expect(api.editDraft).toHaveBeenCalledWith(CONVERSATION_REF, {
        subject: readyDraft.subject,
        message: 'My own wording for this guest.',
      }),
    )

    expect(api.requestGuestReply).not.toHaveBeenCalled()
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('regenerates against the conversation as it stands now', async () => {
    vi.mocked(api.regenerateDraft).mockResolvedValue(readyDraft)

    renderConversation()

    await screen.findByText('Is there parking at the house?')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Regenerate' }))

    await waitFor(() =>
      expect(api.regenerateDraft).toHaveBeenCalledWith(CONVERSATION_REF),
    )

    // Regeneration re-reads the thread rather than trusting its own response,
    // because staleness is judged against the conversation.
    await waitFor(() => expect(api.getConversation).toHaveBeenCalledTimes(2))

    expect(api.requestGuestReply).not.toHaveBeenCalled()
  })

  it('reports a preparation failure instead of an empty box', async () => {
    withDraft(reviewDraft)

    renderConversation()

    expect(
      await screen.findByText(/A reply could not be prepared automatically/),
    ).toBeInTheDocument()
    expect(screen.getByText('Needs human review')).toBeInTheDocument()

    expect(screen.getByLabelText('Message')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Send for approval' })).toBeDisabled()
  })

  it('shows a deliberate silence as a decision, not an empty draft', async () => {
    withDraft(noReplyDraft)

    renderConversation()

    expect(
      await screen.findByText(/The guest closed the conversation, so no reply/),
    ).toBeInTheDocument()
    expect(screen.getByText('No reply needed')).toBeInTheDocument()

    // Nothing was written for a person to accidentally send.
    expect(screen.getByLabelText('Message')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Send for approval' })).toBeDisabled()

    expect(api.requestGuestReply).not.toHaveBeenCalled()
  })

  it('lets the owner overrule a silence by writing their own reply', async () => {
    withDraft(noReplyDraft)
    vi.mocked(api.requestGuestReply).mockResolvedValue(guestReplyWaiting)

    renderConversation()

    await screen.findByText(/The guest closed the conversation, so no reply/)

    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Message'), 'Actually, one more thing.')

    expect(screen.getByRole('button', { name: 'Send for approval' })).toBeEnabled()
  })

  it('says so plainly when nothing has been prepared yet', async () => {
    withDraft(null)

    renderConversation()

    expect(
      await screen.findByText('No reply has been prepared for this conversation yet.'),
    ).toBeInTheDocument()
  })
})

describe('a stale prepared reply', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.getConversation).mockResolvedValue({
      ...conversationDetail,
      draft: staleDraft,
    })
  })

  it('warns that the guest has written again', async () => {
    renderConversation()

    expect(await screen.findByText(/The guest has written again/)).toBeInTheDocument()
    expect(screen.getByText('Draft stale')).toBeInTheDocument()
  })

  it('cannot be sent', async () => {
    renderConversation()

    await screen.findByText(/The guest has written again/)

    expect(screen.getByRole('button', { name: 'Send for approval' })).toBeDisabled()
  })

  it('does not put its text in the editor for a person to send by hand', async () => {
    renderConversation()

    await screen.findByText(/The guest has written again/)

    // The safety property is that the outdated wording is never sendable --
    // not merely that a button is greyed out.
    expect(screen.getByLabelText('Message')).toHaveValue('')
  })

  it('is never submitted, even if the button is reached', async () => {
    renderConversation()

    await screen.findByText(/The guest has written again/)

    const user = userEvent.setup()

    // The click is a no-op rather than merely a disabled button: the guard is
    // in the handler, not only in the markup.
    await user.click(screen.getByRole('button', { name: 'Send for approval' }))

    expect(api.requestGuestReply).not.toHaveBeenCalled()
  })

  it('can be regenerated', async () => {
    vi.mocked(api.regenerateDraft).mockResolvedValue(readyDraft)

    renderConversation()

    await screen.findByText(/The guest has written again/)

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Regenerate' }))

    await waitFor(() =>
      expect(api.regenerateDraft).toHaveBeenCalledWith(CONVERSATION_REF),
    )
  })
})
