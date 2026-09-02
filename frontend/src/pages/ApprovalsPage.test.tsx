import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ApprovalsPage } from './ApprovalsPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { APPROVAL_ID, approvals, resumedResponse } from '../test/factories'

vi.mock('../api/agentguard')

describe('ApprovalsPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('shows pending and resolved approval history', async () => {
    vi.mocked(api.listApprovals).mockResolvedValue(approvals)

    renderWithRouter(<ApprovalsPage />)

    expect(await screen.findByText('PENDING')).toBeInTheDocument()
    expect(screen.getByText('REJECTED')).toBeInTheDocument()
    expect(screen.getAllByText('WRITE')).toHaveLength(2)
  })

  it('offers actions only for pending approvals', async () => {
    vi.mocked(api.listApprovals).mockResolvedValue(approvals)

    renderWithRouter(<ApprovalsPage />)

    await screen.findByText('PENDING')

    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Reject' })).toHaveLength(1)
  })

  it('resolves through the backend and refreshes', async () => {
    vi.mocked(api.listApprovals).mockResolvedValue(approvals)
    vi.mocked(api.resolveApproval).mockResolvedValue(resumedResponse)

    renderWithRouter(<ApprovalsPage />)

    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Approve' }))

    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, true)
    })

    // Reloaded rather than mutating local state.
    await waitFor(() => {
      expect(api.listApprovals).toHaveBeenCalledTimes(2)
    })
  })

  it('filters by status through the backend', async () => {
    vi.mocked(api.listApprovals).mockResolvedValue(approvals)

    renderWithRouter(<ApprovalsPage />)

    await screen.findByText('PENDING')

    const user = userEvent.setup()

    await user.selectOptions(screen.getByLabelText('Status'), 'APPROVED')

    await waitFor(() => {
      expect(api.listApprovals).toHaveBeenCalledWith({ status: 'APPROVED', limit: 100 })
    })
  })

  it('shows an empty state when nothing matches', async () => {
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<ApprovalsPage />)

    expect(await screen.findByText('No approvals to show.')).toBeInTheDocument()
  })
})
