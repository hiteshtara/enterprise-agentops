import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { RunsPage } from './RunsPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { ApiError } from '../api/client'
import { runSummaries } from '../test/factories'

vi.mock('../api/agentguard')

describe('RunsPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('lists runs with status badges', async () => {
    vi.mocked(api.listRuns).mockResolvedValue(runSummaries)

    renderWithRouter(<RunsPage />)

    // Scoped to the table: the status filter lists the same status names.
    const table = within(await screen.findByRole('table'))

    expect(
      table.getByText('Investigate migration batch 43 and restart it if needed.'),
    ).toBeInTheDocument()
    expect(table.getByText('COMPLETED')).toBeInTheDocument()
    expect(table.getByText('WAITING FOR APPROVAL')).toBeInTheDocument()
    expect(table.getByText('Restart batch 51.')).toBeInTheDocument()
  })

  it('shows an empty state when there are no runs', async () => {
    vi.mocked(api.listRuns).mockResolvedValue([])

    renderWithRouter(<RunsPage />)

    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument()
  })

  it('shows a safe error when loading fails', async () => {
    vi.mocked(api.listRuns).mockRejectedValue(
      new ApiError('Cannot reach the AgentGuard API.'),
    )

    renderWithRouter(<RunsPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach')
  })
})
