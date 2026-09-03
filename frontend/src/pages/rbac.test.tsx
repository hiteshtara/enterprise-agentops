/** Role-aware rendering of approval actions. */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AgentPage } from './AgentPage'
import { ApprovalsPage } from './ApprovalsPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { ApiError } from '../api/client'
import {
  APPROVAL_ID,
  approvals,
  approverUser,
  operatorUser,
  completedRunDetail,
  resumedResponse,
  viewerUser,
  waitingResponse,
} from '../test/factories'

vi.mock('../api/agentguard')

const PROMPT = 'Investigate migration batch 43 and restart it if needed.'

async function runToApproval() {
  const user = userEvent.setup()

  await user.type(screen.getByLabelText('Request'), PROMPT)
  await user.click(screen.getByRole('button', { name: 'Run agent' }))
  await screen.findByRole('region', { name: 'Approval required' })

  return user
}

describe('approval card permissions', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.runAgent).mockResolvedValue(waitingResponse)
  })

  it('shows an operator the blocked action but no usable approve control', async () => {
    renderWithRouter(<AgentPage />, { user: operatorUser })

    await runToApproval()

    const card = screen.getByRole('region', { name: 'Approval required' })

    // The operator still sees exactly what was proposed and why it stopped.
    expect(card).toHaveTextContent('restart_migration')
    expect(card).toHaveTextContent('WRITE')

    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
    expect(card).toHaveTextContent('APPROVE_WRITE')
  })

  it('shows an approver working approve and reject controls', async () => {
    renderWithRouter(<AgentPage />, { user: approverUser })

    await runToApproval()

    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })

  it('lets an approver resume the run', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue(resumedResponse)
    vi.mocked(api.getRun).mockResolvedValue(completedRunDetail)
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<AgentPage />, { user: approverUser })

    const user = await runToApproval()

    await user.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, true)
    })

    expect(await screen.findByText('COMPLETED')).toBeInTheDocument()
  })

  it('renders a 403 from the backend as a clear message', async () => {
    // The backend refuses even if the UI were bypassed.
    vi.mocked(api.resolveApproval).mockRejectedValue(
      new ApiError(
        'Resolving a WRITE approval requires the APPROVE_WRITE permission.',
        403,
      ),
    )

    renderWithRouter(<AgentPage />, { user: approverUser })

    const user = await runToApproval()

    await user.click(screen.getByRole('button', { name: 'Approve' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('APPROVE_WRITE')
  })
})

describe('approvals page permissions', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.listApprovals).mockResolvedValue(approvals)
  })

  it('hides row actions from a viewer', async () => {
    renderWithRouter(<ApprovalsPage />, { user: viewerUser })

    await screen.findByText('PENDING')

    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })

  it('hides row actions from an operator', async () => {
    renderWithRouter(<ApprovalsPage />, { user: operatorUser })

    await screen.findByText('PENDING')

    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })

  it('offers row actions to an approver', async () => {
    renderWithRouter(<ApprovalsPage />, { user: approverUser })

    await screen.findByText('PENDING')

    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1)
  })

  it('shows who requested and who resolved each approval', async () => {
    renderWithRouter(<ApprovalsPage />, { user: approverUser })

    const table = await screen.findByRole('table')

    expect(table).toHaveTextContent('Requested by')
    expect(table).toHaveTextContent('Resolved by')
    expect(table).toHaveTextContent('user-operator-1')
    expect(table).toHaveTextContent('user-approver-1')
  })
})
