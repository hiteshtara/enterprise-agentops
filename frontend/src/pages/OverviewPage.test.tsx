import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { OverviewPage } from './OverviewPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { ApiError } from '../api/client'
import { overview } from '../test/factories'

vi.mock('../api/agentguard')

describe('OverviewPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('renders governance statistics from the backend', async () => {
    vi.mocked(api.getOverview).mockResolvedValue(overview)

    renderWithRouter(<OverviewPage />)

    expect(await screen.findByText('Runs today')).toBeInTheDocument()

    const stat = (label: string) =>
      screen.getByText(label).parentElement!.querySelector('.stat-value')!.textContent

    expect(stat('Runs today')).toBe('2')
    expect(stat('Completed')).toBe('3')
    expect(stat('Failed')).toBe('1')
    expect(stat('Waiting for approval')).toBe('1')
    expect(stat('Pending approvals')).toBe('1')
    expect(stat('Tool executions')).toBe('9')
  })

  it('lists recent runs and activity', async () => {
    vi.mocked(api.getOverview).mockResolvedValue(overview)

    renderWithRouter(<OverviewPage />)

    expect(await screen.findByText('Restart batch 51.')).toBeInTheDocument()
    expect(screen.getByText('APPROVAL GRANTED')).toBeInTheDocument()
  })

  it('shows a safe error when the backend is unreachable', async () => {
    vi.mocked(api.getOverview).mockRejectedValue(
      new ApiError('Cannot reach the AgentGuard API.'),
    )

    renderWithRouter(<OverviewPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach')
  })
})
