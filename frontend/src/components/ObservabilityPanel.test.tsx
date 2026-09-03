import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { ObservabilityPanel } from './ObservabilityPanel'
import { renderWithRouter } from '../test/render'
import * as api from '../api/agentguard'
import { ApiError } from '../api/client'
import { RUN_ID, runMetrics, unknownRunMetrics } from '../test/factories'

vi.mock('../api/agentguard')

/** Scoped to the metric strip: table headers reuse the same words. */
function strip() {
  return within(screen.getByRole('group', { name: 'Run metrics' }))
}

function metric(label: string): string | undefined {
  return (
    strip().getByText(label).parentElement?.querySelector('dd')?.textContent ??
    undefined
  )
}

describe('ObservabilityPanel', () => {
  beforeEach(() => vi.resetAllMocks())

  it('renders measured metrics for the run', async () => {
    vi.mocked(api.getRunMetrics).mockResolvedValue(runMetrics)

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    await screen.findByRole('region', { name: 'Observability' })

    expect(metric('Model')).toBe('gpt-5.4-mini')
    expect(metric('Elapsed')).toBe('52.5 s')
    expect(metric('Active execution')).toBe('4.9 s')
    expect(metric('Approval wait')).toBe('47.6 s')
    expect(metric('Model calls')).toBe('3')
    expect(metric('Tool calls')).toBe('2')
    expect(metric('Tool failures')).toBe('1')
    expect(metric('Input tokens')).toBe('3,120')
    expect(metric('Total tokens')).toBe('3,600')
  })

  it('separates elapsed, active execution and approval wait', async () => {
    vi.mocked(api.getRunMetrics).mockResolvedValue(runMetrics)

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    await screen.findByRole('region', { name: 'Observability' })

    // Approval wait dominates elapsed time; execution is the small part.
    expect(metric('Elapsed')).toBe('52.5 s')
    expect(metric('Active execution')).toBe('4.9 s')
    expect(strip().getByText('human decision time')).toBeInTheDocument()
    expect(strip().getByText('model + tool time')).toBeInTheDocument()
  })

  it('labels the cost as an estimate', async () => {
    vi.mocked(api.getRunMetrics).mockResolvedValue(runMetrics)

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    await screen.findByRole('region', { name: 'Observability' })

    expect(metric('Estimated cost')).toBe('$0.001740')
    expect(strip().getByText('estimate, not billing')).toBeInTheDocument()
  })

  it('renders unknown metrics as unavailable, never as zero', async () => {
    vi.mocked(api.getRunMetrics).mockResolvedValue(unknownRunMetrics)

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    await screen.findByRole('region', { name: 'Observability' })

    expect(metric('Input tokens')).toBe('Unavailable')
    expect(metric('Output tokens')).toBe('Unavailable')
    expect(metric('Total tokens')).toBe('Unavailable')
    expect(metric('Approval wait')).toBe('Unavailable')

    // An unpriced model shows a dash, not $0.00.
    expect(metric('Estimated cost')).toBe('$—')
    expect(metric('Estimated cost')).not.toBe('$0.00')
  })

  it('lists each model call with duration, tokens and cost', async () => {
    vi.mocked(api.getRunMetrics).mockResolvedValue(runMetrics)

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    const table = within(await screen.findByRole('table', { name: 'Model calls' }))

    const rows = table.getAllByRole('row').slice(1)

    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('1.4 s')
    expect(rows[0]).toHaveTextContent('1,040')
    expect(rows[0]).toHaveTextContent('$0.000580')

    // The failed call shows why, and no invented tokens.
    expect(rows[1]).toHaveTextContent('FAILED')
    expect(rows[1]).toHaveTextContent('APIConnectionError')
    expect(rows[1]).toHaveTextContent('Unavailable')
    expect(rows[1]).toHaveTextContent('$—')
  })

  it('lists tool executions with durations and failures', async () => {
    vi.mocked(api.getRunMetrics).mockResolvedValue(runMetrics)

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    const table = within(await screen.findByRole('table', { name: 'Tool executions' }))

    const rows = table.getAllByRole('row').slice(1)

    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('FAILED')
    expect(rows[0]).toHaveTextContent('ValueError')
    expect(rows[0]).toHaveTextContent('12 ms')

    expect(rows[1]).toHaveTextContent('COMPLETED')
    expect(rows[1]).toHaveTextContent('688 ms')
    expect(rows[1]).toHaveTextContent('retry 1')
  })

  it('omits the tables when nothing ran', async () => {
    vi.mocked(api.getRunMetrics).mockResolvedValue({
      ...unknownRunMetrics,
      models: [],
      tools: [],
    })

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    await screen.findByRole('region', { name: 'Observability' })

    expect(screen.queryByRole('table', { name: 'Model calls' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('table', { name: 'Tool executions' }),
    ).not.toBeInTheDocument()
  })

  it('shows a loading state', () => {
    vi.mocked(api.getRunMetrics).mockReturnValue(new Promise(() => {}))

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    expect(screen.getByRole('status')).toHaveTextContent('Loading metrics')
  })

  it('shows a safe error when metrics cannot be loaded', async () => {
    vi.mocked(api.getRunMetrics).mockRejectedValue(
      new ApiError('Cannot reach the AgentGuard API.'),
    )

    renderWithRouter(<ObservabilityPanel runId={RUN_ID} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach')
  })
})
