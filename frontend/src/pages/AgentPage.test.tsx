import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AgentPage } from './AgentPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { ApiError } from '../api/client'
import {
  APPROVAL_ID,
  completedResponse,
  rejectedResponse,
  cancelledRunDetail,
  completedRunDetail,
  resumedResponse,
  waitingResponse,
} from '../test/factories'

vi.mock('../api/agentguard')

const PROMPT = 'Investigate migration batch 43 and restart it if needed.'

async function submit(prompt = PROMPT) {
  const user = userEvent.setup()

  await user.type(screen.getByLabelText('Request'), prompt)
  await user.click(screen.getByRole('button', { name: 'Run agent' }))

  return user
}

describe('AgentPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('submits the prompt to the backend', async () => {
    vi.mocked(api.runAgent).mockResolvedValue(completedResponse)

    renderWithRouter(<AgentPage />)
    await submit()

    expect(api.runAgent).toHaveBeenCalledWith(PROMPT)
  })

  it('renders a completed run with its answer and tool activity', async () => {
    vi.mocked(api.runAgent).mockResolvedValue(completedResponse)

    renderWithRouter(<AgentPage />)
    await submit()

    expect(await screen.findByText('COMPLETED')).toBeInTheDocument()
    expect(
      screen.getByText('Batch 43 failed because of an Oracle connection timeout.'),
    ).toBeInTheDocument()
    expect(screen.getByText('query_migration_batches')).toBeInTheDocument()
    expect(screen.getByText(/batch_id/)).toBeInTheDocument()
  })

  it('renders the approval card when the run is waiting', async () => {
    vi.mocked(api.runAgent).mockResolvedValue(waitingResponse)

    renderWithRouter(<AgentPage />)
    await submit()

    expect(await screen.findByText('WAITING FOR APPROVAL')).toBeInTheDocument()

    const card = screen.getByRole('region', { name: 'Approval required' })

    expect(card).toHaveTextContent('restart_migration')
    expect(card).toHaveTextContent('WRITE')
    expect(card).toHaveTextContent('batch_id')
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })

  it('does not show an answer block while approval is pending', async () => {
    vi.mocked(api.runAgent).mockResolvedValue(waitingResponse)

    renderWithRouter(<AgentPage />)
    await submit()

    await screen.findByRole('region', { name: 'Approval required' })

    expect(screen.queryByText('Answer')).not.toBeInTheDocument()
  })

  it('resumes the same run on approve and shows the final answer', async () => {
    vi.mocked(api.runAgent).mockResolvedValue(waitingResponse)
    vi.mocked(api.resolveApproval).mockResolvedValue(resumedResponse)
    // Resolving re-reads the run, so the page shows what was persisted rather
    // than splicing the decision response.
    vi.mocked(api.getRun).mockResolvedValue(completedRunDetail)
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<AgentPage />)
    const user = await submit()

    await user.click(await screen.findByRole('button', { name: 'Approve' }))

    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, true)
    })

    expect(await screen.findByText('COMPLETED')).toBeInTheDocument()
    expect(screen.getByText(/restart was executed successfully/)).toBeInTheDocument()

    // Approval card is gone once resolved.
    expect(
      screen.queryByRole('region', { name: 'Approval required' }),
    ).not.toBeInTheDocument()

    // The pre-approval read tool and the post-approval write tool both remain.
    expect(screen.getByText('query_migration_batches')).toBeInTheDocument()
    expect(screen.getByText('restart_migration')).toBeInTheDocument()
  })

  it('cancels the run on reject without executing the tool', async () => {
    vi.mocked(api.runAgent).mockResolvedValue(waitingResponse)
    vi.mocked(api.resolveApproval).mockResolvedValue(rejectedResponse)
    vi.mocked(api.getRun).mockResolvedValue(cancelledRunDetail)
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<AgentPage />)
    const user = await submit()

    await user.click(await screen.findByRole('button', { name: 'Reject' }))

    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith(APPROVAL_ID, false)
    })

    expect(await screen.findByText('CANCELLED')).toBeInTheDocument()
    expect(screen.getByText(/was not approved/)).toBeInTheDocument()

    // The write tool never executed, so it is absent from the persisted trace.
    expect(screen.queryByText('restart_migration')).not.toBeInTheDocument()
  })

  it('shows a safe message when the backend is unreachable', async () => {
    vi.mocked(api.runAgent).mockRejectedValue(
      new ApiError(
        'Cannot reach the AgentGuard API. Check that the backend is running.',
      ),
    )

    renderWithRouter(<AgentPage />)
    await submit()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Cannot reach the AgentGuard API',
    )
  })

  it('never renders a raw exception message', async () => {
    vi.mocked(api.runAgent).mockRejectedValue(
      new TypeError('Cannot read properties of undefined (reading "output")'),
    )

    renderWithRouter(<AgentPage />)
    await submit()

    const alert = await screen.findByRole('alert')

    expect(alert).toHaveTextContent('Something went wrong')
    expect(alert).not.toHaveTextContent('undefined')
  })
})
