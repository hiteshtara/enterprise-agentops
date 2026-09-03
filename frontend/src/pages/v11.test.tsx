/** Console V1.1: polling, URL filters, run restoration, reconcile, copy link. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AgentPage } from './AgentPage'
import { ApprovalsPage } from './ApprovalsPage'
import { AuditPage } from './AuditPage'
import { RunDetailPage } from './RunDetailPage'
import { RunsPage } from './RunsPage'
import { Route, Routes } from 'react-router-dom'
import { locationSearch } from '../test/location'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { ApiError } from '../api/client'
import {
  RUN_ID,
  adminUser,
  approvals,
  approverUser,
  auditEvents,
  completedRunDetail,
  operatorUser,
  runDetail,
  runSummaries,
  waitingRunDetail,
} from '../test/factories'

vi.mock('../api/agentguard')

function detailRoute() {
  return (
    <Routes>
      <Route path="/runs/:runId" element={<RunDetailPage />} />
    </Routes>
  )
}

// ---------------------------------------------------------------------------
// Active run polling
// ---------------------------------------------------------------------------

describe('active run polling', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('keeps polling a RUNNING run', async () => {
    const running = { ...runDetail, status: 'RUNNING' as const }

    vi.mocked(api.getRun).mockResolvedValue(running)

    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(1))

    await vi.advanceTimersByTimeAsync(3100)
    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(2))

    await vi.advanceTimersByTimeAsync(3100)
    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(3))
  })

  it('keeps polling a WAITING_FOR_APPROVAL run', async () => {
    vi.mocked(api.getRun).mockResolvedValue(waitingRunDetail)

    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(1))

    await vi.advanceTimersByTimeAsync(3100)
    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(2))
  })

  it('stops polling once the run reaches a terminal status', async () => {
    vi.mocked(api.getRun).mockResolvedValue(completedRunDetail)

    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(1))

    await vi.advanceTimersByTimeAsync(12_000)

    expect(api.getRun).toHaveBeenCalledTimes(1)
  })

  it('picks up a status change without a manual reload', async () => {
    vi.mocked(api.getRun)
      .mockResolvedValueOnce(waitingRunDetail)
      .mockResolvedValue(completedRunDetail)

    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    expect(await screen.findByText('WAITING FOR APPROVAL')).toBeInTheDocument()

    await vi.advanceTimersByTimeAsync(3100)

    expect(await screen.findByText('COMPLETED')).toBeInTheDocument()
  })

  it('stops polling when the component unmounts', async () => {
    const running = { ...runDetail, status: 'RUNNING' as const }

    vi.mocked(api.getRun).mockResolvedValue(running)

    const view = renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    await waitFor(() => expect(api.getRun).toHaveBeenCalledTimes(1))

    view.unmount()

    await vi.advanceTimersByTimeAsync(12_000)

    expect(api.getRun).toHaveBeenCalledTimes(1)
  })

  it('does not poll a run list with nothing active', async () => {
    vi.mocked(api.listRuns).mockResolvedValue([runSummaries[0]])

    renderWithRouter(<RunsPage />)

    await waitFor(() => expect(api.listRuns).toHaveBeenCalledTimes(1))

    await vi.advanceTimersByTimeAsync(12_000)

    expect(api.listRuns).toHaveBeenCalledTimes(1)
  })

  it('polls a run list that still has an active run', async () => {
    vi.mocked(api.listRuns).mockResolvedValue(runSummaries)

    renderWithRouter(<RunsPage />)

    await waitFor(() => expect(api.listRuns).toHaveBeenCalledTimes(1))

    await vi.advanceTimersByTimeAsync(4100)

    await waitFor(() => expect(api.listRuns).toHaveBeenCalledTimes(2))
  })
})

// ---------------------------------------------------------------------------
// Agent page restoration
// ---------------------------------------------------------------------------

describe('agent run restoration', () => {
  beforeEach(() => vi.resetAllMocks())

  it('restores a run named in the URL without resubmitting', async () => {
    vi.mocked(api.getRun).mockResolvedValue(completedRunDetail)
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<AgentPage />, `/agent?run=${RUN_ID}`)

    expect(await screen.findByText('COMPLETED')).toBeInTheDocument()
    expect(
      screen.getByText('Investigate migration batch 43 and restart it if needed.'),
    ).toBeInTheDocument()

    // The run is rebuilt from durable state, not re-run.
    expect(api.getRun).toHaveBeenCalledWith(RUN_ID)
    expect(api.runAgent).not.toHaveBeenCalled()
  })

  it('rebuilds the tool trace from persisted steps', async () => {
    vi.mocked(api.getRun).mockResolvedValue(completedRunDetail)
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<AgentPage />, `/agent?run=${RUN_ID}`)

    expect(await screen.findByText('query_migration_batches')).toBeInTheDocument()
    expect(screen.getByText('restart_migration')).toBeInTheDocument()
  })

  it('rebuilds a pending approval card after navigating back', async () => {
    vi.mocked(api.getRun).mockResolvedValue(waitingRunDetail)
    vi.mocked(api.listApprovals).mockResolvedValue([approvals[0]])

    renderWithRouter(<AgentPage />, {
      route: `/agent?run=${RUN_ID}`,
      user: approverUser,
    })

    const card = await screen.findByRole('region', { name: 'Approval required' })

    expect(card).toHaveTextContent('restart_migration')
    expect(api.listApprovals).toHaveBeenCalledWith({
      runId: RUN_ID,
      status: 'PENDING',
      limit: 1,
    })
  })

  it('shows nothing to restore without a run parameter', async () => {
    renderWithRouter(<AgentPage />, '/agent')

    expect(await screen.findByLabelText('Request')).toBeInTheDocument()
    expect(api.getRun).not.toHaveBeenCalled()
  })

  it('puts the run id in the URL after submitting', async () => {
    vi.mocked(api.runAgent).mockResolvedValue({
      ...completedRunDetail,
      status: 'COMPLETED',
      answer: 'Done.',
      trace: [],
      approval_required: null,
    } as never)
    vi.mocked(api.getRun).mockResolvedValue(completedRunDetail)
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<AgentPage />, '/agent')

    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Request'), 'Show me a batch.')
    await user.click(screen.getByRole('button', { name: 'Run agent' }))

    await waitFor(() => expect(locationSearch()).toContain(`run=${RUN_ID}`))
  })
})

// ---------------------------------------------------------------------------
// URL-backed filters
// ---------------------------------------------------------------------------

describe('url-backed filters', () => {
  beforeEach(() => vi.resetAllMocks())

  it('reads the Runs status filter from the URL', async () => {
    vi.mocked(api.listRuns).mockResolvedValue([])

    renderWithRouter(<RunsPage />, '/runs?status=WAITING_FOR_APPROVAL')

    await waitFor(() => {
      expect(api.listRuns).toHaveBeenCalledWith({
        status: 'WAITING_FOR_APPROVAL',
        limit: 50,
      })
    })

    expect(screen.getByLabelText('Status')).toHaveValue('WAITING_FOR_APPROVAL')
  })

  it('writes the Runs status filter into the URL', async () => {
    vi.mocked(api.listRuns).mockResolvedValue(runSummaries)

    renderWithRouter(<RunsPage />, '/runs')

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.selectOptions(screen.getByLabelText('Status'), 'FAILED')

    await waitFor(() => expect(locationSearch()).toContain('status=FAILED'))
  })

  it('ignores an unrecognised status in the URL', async () => {
    vi.mocked(api.listRuns).mockResolvedValue([])

    renderWithRouter(<RunsPage />, '/runs?status=BOGUS')

    await waitFor(() => {
      expect(api.listRuns).toHaveBeenCalledWith({ status: undefined, limit: 50 })
    })
  })

  it('reads the Approvals status filter from the URL', async () => {
    vi.mocked(api.listApprovals).mockResolvedValue([])

    renderWithRouter(<ApprovalsPage />, '/approvals?status=PENDING')

    await waitFor(() => {
      expect(api.listApprovals).toHaveBeenCalledWith({ status: 'PENDING', limit: 100 })
    })

    expect(screen.getByLabelText('Status')).toHaveValue('PENDING')
  })

  it('reads the Audit event_type filter from the URL', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />, '/audit?event_type=TOOL_FAILED')

    await waitFor(() => {
      expect(api.listAuditEvents).toHaveBeenCalledWith({
        runId: undefined,
        eventType: 'TOOL_FAILED',
        limit: 200,
      })
    })

    expect(screen.getByLabelText('Event type')).toHaveValue('TOOL_FAILED')
  })

  it('reads the Audit run_id filter from the URL', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />, `/audit?run_id=${RUN_ID}`)

    await waitFor(() => {
      expect(api.listAuditEvents).toHaveBeenCalledWith({
        runId: RUN_ID,
        eventType: undefined,
        limit: 200,
      })
    })

    expect(screen.getByLabelText('Run ID')).toHaveValue(RUN_ID)
  })

  it('combines both audit filters from the URL', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />, `/audit?run_id=${RUN_ID}&event_type=TOOL_EXECUTED`)

    await waitFor(() => {
      expect(api.listAuditEvents).toHaveBeenCalledWith({
        runId: RUN_ID,
        eventType: 'TOOL_EXECUTED',
        limit: 200,
      })
    })
  })

  it('writes an applied audit run filter into the URL', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />, '/audit')

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Run ID'), 'run-xyz')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => expect(locationSearch()).toContain('run_id=run-xyz'))
  })
})

// ---------------------------------------------------------------------------
// Reconciliation
// ---------------------------------------------------------------------------

describe('reconcile stale runs', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.listRuns).mockResolvedValue(runSummaries)
  })

  it('is hidden from a role without RECONCILE_RUNS', async () => {
    renderWithRouter(<RunsPage />, { user: operatorUser })

    await screen.findByRole('table')

    expect(
      screen.queryByRole('button', { name: 'Reconcile stale runs' }),
    ).not.toBeInTheDocument()
  })

  it('is offered to an admin', async () => {
    renderWithRouter(<RunsPage />, { user: adminUser })

    await screen.findByRole('table')

    expect(
      screen.getByRole('button', { name: 'Reconcile stale runs' }),
    ).toBeInTheDocument()
  })

  it('explains what will happen before acting', async () => {
    renderWithRouter(<RunsPage />, { user: adminUser })

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Reconcile stale runs' }))

    const dialog = screen.getByRole('dialog', { name: 'Confirm reconciliation' })

    expect(dialog).toHaveTextContent(/marked FAILED as interrupted/)
    // Nothing has been sent yet.
    expect(api.reconcileRuns).not.toHaveBeenCalled()
  })

  it('reports how many runs were reconciled and refreshes', async () => {
    vi.mocked(api.reconcileRuns).mockResolvedValue({
      count: 2,
      reconciled: [],
    })

    renderWithRouter(<RunsPage />, { user: adminUser })

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Reconcile stale runs' }))
    await user.click(screen.getByRole('button', { name: 'Reconcile' }))

    expect(
      await screen.findByText(/Marked 2 interrupted runs as FAILED/),
    ).toBeInTheDocument()

    await waitFor(() => expect(api.listRuns).toHaveBeenCalledTimes(2))
  })

  it('says so when nothing was stale', async () => {
    vi.mocked(api.reconcileRuns).mockResolvedValue({ count: 0, reconciled: [] })

    renderWithRouter(<RunsPage />, { user: adminUser })

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Reconcile stale runs' }))
    await user.click(screen.getByRole('button', { name: 'Reconcile' }))

    expect(await screen.findByText(/No stale runs found/)).toBeInTheDocument()
  })

  it('renders a backend 403 clearly', async () => {
    vi.mocked(api.reconcileRuns).mockRejectedValue(
      new ApiError('This action requires the RECONCILE_RUNS permission.', 403),
    )

    renderWithRouter(<RunsPage />, { user: adminUser })

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Reconcile stale runs' }))
    await user.click(screen.getByRole('button', { name: 'Reconcile' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('RECONCILE_RUNS')
  })

  it('sends the chosen threshold', async () => {
    vi.mocked(api.reconcileRuns).mockResolvedValue({ count: 0, reconciled: [] })

    renderWithRouter(<RunsPage />, { user: adminUser })

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Reconcile stale runs' }))
    await user.selectOptions(screen.getByLabelText('Stale after'), '3600')
    await user.click(screen.getByRole('button', { name: 'Reconcile' }))

    await waitFor(() => expect(api.reconcileRuns).toHaveBeenCalledWith(3600))
  })

  it('defaults to the backend threshold', async () => {
    vi.mocked(api.reconcileRuns).mockResolvedValue({ count: 0, reconciled: [] })

    renderWithRouter(<RunsPage />, { user: adminUser })

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Reconcile stale runs' }))
    await user.click(screen.getByRole('button', { name: 'Reconcile' }))

    await waitFor(() => expect(api.reconcileRuns).toHaveBeenCalledWith(900))
  })
})

// ---------------------------------------------------------------------------
// Run stats + copy link
// ---------------------------------------------------------------------------

describe('run detail statistics and sharing', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.getRun).mockResolvedValue(completedRunDetail)
  })

  it('renders counts derived from the run steps', async () => {
    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    const panel = within(await screen.findByRole('region', { name: 'Run statistics' }))

    const stat = (label: string) =>
      panel.getByText(label).parentElement?.querySelector('dd')?.textContent

    expect(stat('Steps')).toBe('6')
    expect(stat('Model turns')).toBe('1')
    expect(stat('Tools requested')).toBe('1')
    expect(stat('Tools executed')).toBe('2')
    expect(stat('Tool failures')).toBe('0')
    expect(stat('Approvals requested')).toBe('1')
    expect(stat('Approvals resolved')).toBe('1')
  })

  it('shows elapsed time and approval wait derived from timestamps', async () => {
    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    const panel = within(await screen.findByRole('region', { name: 'Run statistics' }))

    expect(panel.getByText('Elapsed')).toBeInTheDocument()
    expect(panel.getByText('Approval wait')).toBeInTheDocument()
  })

  it('marks the panel as derived rather than measured', async () => {
    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    expect(await screen.findByText(/Derived statistics/)).toBeInTheDocument()
  })

  it('copies the run link and confirms it accessibly', async () => {
    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    // userEvent.setup() installs its own clipboard stub, so ours goes on after.
    const user = userEvent.setup()

    const writeText = vi.fn().mockResolvedValue(undefined)

    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })

    await user.click(await screen.findByRole('button', { name: 'Copy run link' }))

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(window.location.href))

    // Announced via a live region, not just a colour change.
    const feedback = await screen.findByText('Link copied')

    expect(feedback).toHaveAttribute('aria-live', 'polite')
  })

  it('reports a copy failure instead of claiming success', async () => {
    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    const user = userEvent.setup()

    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })

    // The execCommand fallback also fails, so the button must not claim success.
    document.execCommand = vi.fn().mockReturnValue(false)

    await user.click(await screen.findByRole('button', { name: 'Copy run link' }))

    expect(await screen.findByText('Could not copy link')).toBeInTheDocument()
  })

  it('shows a failed run reason under a failure heading', async () => {
    vi.mocked(api.getRun).mockResolvedValue({
      ...completedRunDetail,
      status: 'FAILED',
      final_answer: 'Run interrupted before completion',
    })

    renderWithRouter(detailRoute(), `/runs/${RUN_ID}`)

    expect(await screen.findByText('Failure reason')).toBeInTheDocument()
    expect(screen.queryByText('Final answer')).not.toBeInTheDocument()
  })
})
