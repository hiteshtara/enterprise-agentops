import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AuditPage } from './AuditPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { RUN_ID, auditEvents } from '../test/factories'

vi.mock('../api/agentguard')

describe('AuditPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('lists audit events with type badges and tool names', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />)

    // Scoped to the table: the filter dropdown lists the same event names.
    const table = within(await screen.findByRole('table'))

    expect(table.getByText('APPROVAL GRANTED')).toBeInTheDocument()
    expect(table.getByText('TOOL FAILED')).toBeInTheDocument()
    expect(table.getByText('restart_migration')).toBeInTheDocument()
  })

  it('keeps raw details in a collapsed panel', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />)

    const panels = await screen.findAllByText('Raw details')

    expect(panels).toHaveLength(2)
    expect(panels[0].closest('details')).not.toHaveAttribute('open')
  })

  it('filters by event type', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />)

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.selectOptions(screen.getByLabelText('Event type'), 'TOOL_FAILED')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => {
      expect(api.listAuditEvents).toHaveBeenLastCalledWith({
        runId: undefined,
        eventType: 'TOOL_FAILED',
        limit: 200,
      })
    })
  })

  it('filters by run id', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />)

    await screen.findByRole('table')

    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Run ID'), 'run-xyz')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => {
      expect(api.listAuditEvents).toHaveBeenLastCalledWith({
        runId: 'run-xyz',
        eventType: undefined,
        limit: 200,
      })
    })
  })

  it('preloads the run filter from the query string', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue(auditEvents)

    renderWithRouter(<AuditPage />, `/audit?run_id=${RUN_ID}`)

    await waitFor(() => {
      expect(api.listAuditEvents).toHaveBeenCalledWith({
        runId: RUN_ID,
        eventType: undefined,
        limit: 200,
      })
    })
  })

  it('shows an empty state when nothing matches', async () => {
    vi.mocked(api.listAuditEvents).mockResolvedValue([])

    renderWithRouter(<AuditPage />)

    expect(await screen.findByText(/No audit events match/)).toBeInTheDocument()
  })
})
