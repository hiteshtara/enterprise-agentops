import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { RunDetailPage } from './RunDetailPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { RUN_ID, runDetail } from '../test/factories'

vi.mock('../api/agentguard')

function renderDetail() {
  return renderWithRouter(
    <Routes>
      <Route path="/runs/:runId" element={<RunDetailPage />} />
    </Routes>,
    `/runs/${RUN_ID}`,
  )
}

describe('RunDetailPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('loads the run named in the route', async () => {
    vi.mocked(api.getRun).mockResolvedValue(runDetail)

    renderDetail()

    expect(await screen.findByText('COMPLETED')).toBeInTheDocument()
    expect(api.getRun).toHaveBeenCalledWith(RUN_ID)
  })

  it('shows the original request and final answer', async () => {
    vi.mocked(api.getRun).mockResolvedValue(runDetail)

    renderDetail()

    expect(
      await screen.findByText(
        'Investigate migration batch 43 and restart it if needed.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('Batch 43 was restarted.')).toBeInTheDocument()
  })

  it('renders every step type in the timeline in order', async () => {
    vi.mocked(api.getRun).mockResolvedValue(runDetail)

    renderDetail()

    const timeline = await screen.findByRole('list', { name: 'Run timeline' })
    const rows = within(timeline).getAllByRole('listitem')

    expect(rows).toHaveLength(6)
    expect(timeline).toHaveTextContent('MODEL RESPONSE')
    expect(timeline).toHaveTextContent('TOOL REQUESTED')
    expect(timeline).toHaveTextContent('TOOL EXECUTED')
    expect(timeline).toHaveTextContent('APPROVAL REQUIRED')
    expect(timeline).toHaveTextContent('APPROVAL GRANTED')

    // Ordered by step number.
    expect(rows[0]).toHaveTextContent('step 1')
    expect(rows[5]).toHaveTextContent('step 6')
  })

  it('distinguishes the approval steps from tool steps', async () => {
    vi.mocked(api.getRun).mockResolvedValue(runDetail)

    renderDetail()

    const timeline = await screen.findByRole('list', { name: 'Run timeline' })
    const approvalRow = within(timeline)
      .getAllByRole('listitem')
      .find((row) => row.textContent?.includes('APPROVAL REQUIRED'))

    expect(approvalRow).toBeDefined()
    expect(approvalRow).toHaveTextContent('restart_migration')
  })
})
