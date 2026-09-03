import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { KnowledgePage } from './KnowledgePage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import {
  KNOWLEDGE_REF,
  approvedInternal,
  approverUser,
  globalCandidate,
  internalCandidate,
  knowledgePage,
  numericCandidate,
  parkingConflict,
  readyCandidate,
  rejectedItem,
  supersededItem,
} from '../test/factories'

vi.mock('../api/agentguard')

const QUEUE = [readyCandidate, numericCandidate, internalCandidate]

function renderKnowledge(user?: Parameters<typeof renderWithRouter>[1]) {
  return renderWithRouter(<KnowledgePage />, user ?? {})
}

describe('KnowledgePage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.listKnowledge).mockResolvedValue(knowledgePage(QUEUE))
  })

  // -- list and grouping ---------------------------------------------------

  it('opens on the review queue', async () => {
    renderKnowledge()

    await screen.findByText('Shared parking')

    expect(api.listKnowledge).toHaveBeenCalledWith({ status: 'PROPOSED' })
  })

  it('separates ready, numeric and internal candidates', async () => {
    renderKnowledge()

    expect(await screen.findByText('Ready for review (1)')).toBeInTheDocument()
    expect(screen.getByText('Numeric / stale-fact review (1)')).toBeInTheDocument()
    expect(screen.getByText('Internal operations (1)')).toBeInTheDocument()
  })

  it('shows evidence, scope and topic for a candidate', async () => {
    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Shared parking' })

    expect(within(card).getByText('parking')).toBeInTheDocument()
    expect(within(card).getByText('renovated-2nd-floor-home')).toBeInTheDocument()
    expect(within(card).getByText(/6 replies across 1 property/)).toBeInTheDocument()
  })

  it('shows GLOBAL for a rule with no property', async () => {
    vi.mocked(api.listKnowledge).mockResolvedValue(knowledgePage([globalCandidate]))

    renderKnowledge()

    const card = await screen.findByRole('region', {
      name: 'Early check-in is not guaranteed',
    })

    expect(within(card).getByText('GLOBAL')).toBeInTheDocument()
    expect(within(card).getByText(/across 3 properties/)).toBeInTheDocument()
  })

  it('badges the audience of each candidate', async () => {
    renderKnowledge()

    await screen.findByText('Shared parking')

    expect(screen.getAllByText('Guest-facing')).toHaveLength(2)
    expect(screen.getAllByText('Internal')).toHaveLength(1)
  })

  it('never renders historical guest messages', async () => {
    const { container } = renderKnowledge()

    await screen.findByText('Shared parking')

    expect(container.textContent).not.toMatch(/evidence_refs/)
    expect(container.textContent).not.toMatch(/@/)
  })

  // -- status tabs ---------------------------------------------------------

  it.each([
    ['APPROVED', approvedInternal, 'Internal booking record'],
    ['REJECTED', rejectedItem, 'Rejected wording'],
    ['SUPERSEDED', supersededItem, 'Older parking wording'],
  ])('lists %s knowledge', async (status, item, title) => {
    vi.mocked(api.listKnowledge).mockResolvedValue(knowledgePage([item]))

    renderKnowledge()

    const user = userEvent.setup()

    await user.click(await screen.findByRole('tab', { name: new RegExp(status) }))

    await waitFor(() => expect(api.listKnowledge).toHaveBeenCalledWith({ status }))

    expect(await screen.findByText(title)).toBeInTheDocument()
  })

  // -- warnings ------------------------------------------------------------

  it('warns that a numeric candidate may be stale', async () => {
    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Transit access' })

    expect(within(card).getByText('Check this number')).toBeInTheDocument()
    expect(
      within(card).getByText(/potentially stale operational number/),
    ).toBeInTheDocument()
    expect(within(card).getByText(/Flagged: distance/)).toBeInTheDocument()
  })

  it('marks internal candidates as never used in guest replies', async () => {
    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Calendar stays blocked' })

    expect(within(card).getByText(/Not used in guest replies/)).toBeInTheDocument()
  })

  it('keeps the not-used label on an approved internal rule', async () => {
    vi.mocked(api.listKnowledge).mockResolvedValue(knowledgePage([approvedInternal]))

    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Internal booking record' })

    expect(within(card).getByText(/Not used in guest replies/)).toBeInTheDocument()
    expect(
      within(card).getByText(/stays out of guest-facing drafting even once approved/),
    ).toBeInTheDocument()
  })

  it('surfaces approved-knowledge conflicts without resolving them', async () => {
    vi.mocked(api.listKnowledge).mockResolvedValue(
      knowledgePage(QUEUE, [parkingConflict]),
    )

    renderKnowledge()

    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('1 approved-knowledge conflict')
    expect(alert).toHaveTextContent('More than one approved rule covers this topic')
  })

  // -- decisions -----------------------------------------------------------

  it('approves a candidate and re-reads from the backend', async () => {
    vi.mocked(api.decideKnowledge).mockResolvedValue(readyCandidate)

    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Shared parking' })
    const user = userEvent.setup()

    await user.click(within(card).getByRole('button', { name: 'Approve' }))

    await waitFor(() =>
      expect(api.decideKnowledge).toHaveBeenCalledWith(KNOWLEDGE_REF, 'approve'),
    )

    await waitFor(() => expect(api.listKnowledge).toHaveBeenCalledTimes(2))
  })

  it('rejects a candidate', async () => {
    vi.mocked(api.decideKnowledge).mockResolvedValue(readyCandidate)

    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Shared parking' })
    const user = userEvent.setup()

    await user.click(within(card).getByRole('button', { name: 'Reject' }))

    await waitFor(() =>
      expect(api.decideKnowledge).toHaveBeenCalledWith(KNOWLEDGE_REF, 'reject'),
    )
  })

  it('edits a candidate without approving it', async () => {
    vi.mocked(api.editKnowledge).mockResolvedValue(readyCandidate)

    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Shared parking' })
    const user = userEvent.setup()

    await user.click(within(card).getByRole('button', { name: 'Edit' }))

    const box = within(card).getByLabelText('Rule')

    await user.clear(box)
    await user.type(box, 'Parking is shared and must be confirmed on the day.')
    await user.click(within(card).getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(api.editKnowledge).toHaveBeenCalledWith(KNOWLEDGE_REF, {
        content: 'Parking is shared and must be confirmed on the day.',
      }),
    )

    // Editing is not approving.
    expect(api.decideKnowledge).not.toHaveBeenCalled()
  })

  it('approves the edited wording after saving it', async () => {
    const edited = {
      ...readyCandidate,
      content: 'Parking is shared and must be confirmed on the day.',
    }

    vi.mocked(api.editKnowledge).mockResolvedValue(edited)
    vi.mocked(api.decideKnowledge).mockResolvedValue({ ...edited, status: 'APPROVED' })
    vi.mocked(api.listKnowledge)
      .mockResolvedValueOnce(knowledgePage(QUEUE))
      .mockResolvedValue(knowledgePage([edited, numericCandidate, internalCandidate]))

    renderKnowledge()

    const user = userEvent.setup()
    let card = await screen.findByRole('region', { name: 'Shared parking' })

    await user.click(within(card).getByRole('button', { name: 'Edit' }))

    const box = within(card).getByLabelText('Rule')

    await user.clear(box)
    await user.type(box, 'Parking is shared and must be confirmed on the day.')
    await user.click(within(card).getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(
        screen.getByText('Parking is shared and must be confirmed on the day.'),
      ).toBeInTheDocument(),
    )

    card = screen.getByRole('region', { name: 'Shared parking' })

    await user.click(within(card).getByRole('button', { name: 'Approve' }))

    await waitFor(() =>
      expect(api.decideKnowledge).toHaveBeenCalledWith(KNOWLEDGE_REF, 'approve'),
    )
  })

  it('supersedes an approved rule instead of editing it in place', async () => {
    vi.mocked(api.listKnowledge).mockResolvedValue(
      knowledgePage([{ ...readyCandidate, status: 'APPROVED' }]),
    )
    vi.mocked(api.supersedeKnowledge).mockResolvedValue(readyCandidate)

    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Shared parking' })
    const user = userEvent.setup()

    await user.click(within(card).getByRole('button', { name: 'Edit' }))

    const box = within(card).getByLabelText('Rule')

    await user.clear(box)
    await user.type(box, 'Parking is shared; confirm before arrival.')
    await user.click(within(card).getByRole('button', { name: 'Save as new version' }))

    await waitFor(() =>
      expect(api.supersedeKnowledge).toHaveBeenCalledWith(KNOWLEDGE_REF, {
        content: 'Parking is shared; confirm before arrival.',
      }),
    )

    expect(api.editKnowledge).not.toHaveBeenCalled()
  })

  // -- manual knowledge ----------------------------------------------------

  it('offers a form for owner-authored knowledge', async () => {
    renderKnowledge()

    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Add knowledge' }))

    expect(screen.getByLabelText('Title')).toBeInTheDocument()
    expect(screen.getByLabelText('Topic')).toBeInTheDocument()
    expect(screen.getByLabelText('Audience')).toBeInTheDocument()
    expect(
      screen.getByLabelText('Property slug (leave blank for GLOBAL)'),
    ).toBeInTheDocument()
  })

  it('creates owner-authored knowledge', async () => {
    vi.mocked(api.createKnowledge).mockResolvedValue(readyCandidate)

    renderKnowledge()

    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Add knowledge' }))

    const form = screen.getByRole('region', { name: 'Add knowledge' })

    await user.type(within(form).getByLabelText('Title'), 'Quiet hours')
    await user.type(within(form).getByLabelText('Topic'), 'house_rules')
    await user.type(
      within(form).getByLabelText('Rule'),
      'Please keep noise down after 10 in the evening.',
    )

    await user.click(within(form).getByRole('button', { name: 'Create' }))

    await waitFor(() =>
      expect(api.createKnowledge).toHaveBeenCalledWith({
        property_slug: null,
        topic: 'house_rules',
        title: 'Quiet hours',
        content: 'Please keep noise down after 10 in the evening.',
        audience: 'GUEST_FACING',
      }),
    )
  })

  // -- RBAC ----------------------------------------------------------------

  it('hides mutation controls from a non-admin', async () => {
    renderKnowledge({ user: approverUser })

    await screen.findByText('Shared parking')

    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Add knowledge' }),
    ).not.toBeInTheDocument()

    expect(
      screen.getAllByText(/Your role can read knowledge but not change it/),
    ).not.toHaveLength(0)
  })

  it('reports a refused decision without leaking server detail', async () => {
    const { ApiError } = await import('../api/client')

    vi.mocked(api.decideKnowledge).mockRejectedValue(
      new ApiError('This action requires the ADMINISTER permission.', 403),
    )

    renderKnowledge()

    const card = await screen.findByRole('region', { name: 'Shared parking' })
    const user = userEvent.setup()

    await user.click(within(card).getByRole('button', { name: 'Approve' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This action requires the ADMINISTER permission.',
    )
  })

  // -- states --------------------------------------------------------------

  it('shows a loading state', () => {
    vi.mocked(api.listKnowledge).mockReturnValue(new Promise(() => {}))

    renderKnowledge()

    expect(screen.getByRole('status')).toHaveTextContent('Loading knowledge')
  })

  it('shows an empty state', async () => {
    vi.mocked(api.listKnowledge).mockResolvedValue(knowledgePage([]))

    renderKnowledge()

    expect(await screen.findByText('No proposed knowledge.')).toBeInTheDocument()
  })

  it('shows a generic error for an unexpected failure', async () => {
    vi.mocked(api.listKnowledge).mockRejectedValue(new Error('socket hang up'))

    renderKnowledge()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Something went wrong loading this view.',
    )
  })
})
