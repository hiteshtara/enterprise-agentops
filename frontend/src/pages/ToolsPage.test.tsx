import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { ToolsPage } from './ToolsPage'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { tools } from '../test/factories'

vi.mock('../api/agentguard')

describe('ToolsPage', () => {
  beforeEach(() => vi.resetAllMocks())

  it('renders a risk badge for every tier', async () => {
    vi.mocked(api.listTools).mockResolvedValue(tools)

    renderWithRouter(<ToolsPage />)

    expect(await screen.findByText('READ')).toBeInTheDocument()
    expect(screen.getByText('WRITE')).toBeInTheDocument()
    expect(screen.getByText('DANGEROUS')).toBeInTheDocument()
  })

  it('states the governance policy for each risk tier', async () => {
    vi.mocked(api.listTools).mockResolvedValue(tools)

    renderWithRouter(<ToolsPage />)

    const readRow = (await screen.findByText('query_migration_batches')).closest('tr')!
    const writeRow = screen.getByText('restart_migration').closest('tr')!

    expect(within(readRow).getByText('Executes immediately')).toBeInTheDocument()
    expect(within(writeRow).getByText('Requires human approval')).toBeInTheDocument()
  })

  it('exposes the parameter schema in an expandable panel', async () => {
    vi.mocked(api.listTools).mockResolvedValue(tools)

    renderWithRouter(<ToolsPage />)

    const panels = await screen.findAllByText('Parameter schema')

    expect(panels).toHaveLength(3)
  })

  it('shows an empty state when no tools are registered', async () => {
    vi.mocked(api.listTools).mockResolvedValue([])

    renderWithRouter(<ToolsPage />)

    expect(await screen.findByText('No tools registered.')).toBeInTheDocument()
  })
})
