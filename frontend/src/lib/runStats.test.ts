import { describe, expect, it } from 'vitest'
import { formatDuration, runDurationMs, runStats } from './runStats'
import type { RunStep } from '../api/types'

function step(
  step_number: number,
  step_type: RunStep['step_type'],
  created_at: string,
): RunStep {
  return {
    step_number,
    step_type,
    tool_name: null,
    arguments: null,
    result: null,
    error: null,
    created_at,
  }
}

const run = {
  created_at: '2026-09-02T10:00:00.000+00:00',
  updated_at: '2026-09-02T10:02:05.000+00:00',
}

describe('runStats', () => {
  it('counts tool executions, failures and model turns', () => {
    const stats = runStats(run, [
      step(1, 'MODEL_RESPONSE', run.created_at),
      step(2, 'TOOL_REQUESTED', run.created_at),
      step(3, 'TOOL_FAILED', run.created_at),
      step(4, 'TOOL_EXECUTED', run.created_at),
      step(5, 'MODEL_RESPONSE', run.created_at),
      step(6, 'TOOL_EXECUTED', run.created_at),
    ])

    expect(stats.toolExecutions).toBe(2)
    expect(stats.toolFailures).toBe(1)
    expect(stats.modelTurns).toBe(2)
  })

  it('measures run duration from created_at to updated_at', () => {
    expect(runDurationMs(run)).toBe(125_000)
  })

  it('measures how long a human took to decide', () => {
    const stats = runStats(run, [
      step(1, 'APPROVAL_REQUIRED', '2026-09-02T10:00:10.000+00:00'),
      step(2, 'APPROVAL_GRANTED', '2026-09-02T10:01:40.000+00:00'),
    ])

    expect(stats.approvalWaitMs).toBe(90_000)
  })

  it('measures the wait for a rejected approval too', () => {
    const stats = runStats(run, [
      step(1, 'APPROVAL_REQUIRED', '2026-09-02T10:00:10.000+00:00'),
      step(2, 'APPROVAL_DENIED', '2026-09-02T10:00:25.000+00:00'),
    ])

    expect(stats.approvalWaitMs).toBe(15_000)
  })

  it('reports no approval wait when the run never paused', () => {
    expect(
      runStats(run, [step(1, 'TOOL_EXECUTED', run.created_at)]).approvalWaitMs,
    ).toBeNull()
  })

  it('reports null rather than a negative or invalid duration', () => {
    expect(
      runDurationMs({ created_at: 'nonsense', updated_at: run.updated_at }),
    ).toBeNull()
    expect(
      runDurationMs({ created_at: run.updated_at, updated_at: run.created_at }),
    ).toBeNull()
  })
})

describe('formatDuration', () => {
  it('renders sub-second, second and minute scales', () => {
    expect(formatDuration(840)).toBe('840ms')
    expect(formatDuration(4200)).toBe('4.2s')
    expect(formatDuration(78_000)).toBe('1m 18s')
  })

  it('renders an em dash when unknown', () => {
    expect(formatDuration(null)).toBe('—')
  })
})
