// Presentation arithmetic over a run's timeline. No policy lives here -- these
// are derived numbers the console displays, not decisions it makes.

import type { RunStep, RunSummary } from '../api/types'

export interface RunStats {
  toolExecutions: number
  toolFailures: number
  modelTurns: number
  /** Wall-clock ms from run start to last update, or null if unparsable. */
  durationMs: number | null
  /** Ms a human took to decide, or null when no approval happened. */
  approvalWaitMs: number | null
}

function ms(iso: string | null | undefined): number | null {
  if (!iso) return null

  const parsed = Date.parse(iso)

  return Number.isNaN(parsed) ? null : parsed
}

function between(from: string | undefined, to: string | undefined): number | null {
  const start = ms(from)
  const end = ms(to)

  if (start === null || end === null) return null

  const elapsed = end - start

  return elapsed >= 0 ? elapsed : null
}

export function runDurationMs(run: Pick<RunSummary, 'created_at' | 'updated_at'>) {
  return between(run.created_at, run.updated_at)
}

export function runStats(
  run: Pick<RunSummary, 'created_at' | 'updated_at'>,
  steps: RunStep[],
): RunStats {
  const requiredAt = steps.find((s) => s.step_type === 'APPROVAL_REQUIRED')?.created_at

  const resolvedAt = steps.find(
    (s) => s.step_type === 'APPROVAL_GRANTED' || s.step_type === 'APPROVAL_DENIED',
  )?.created_at

  return {
    toolExecutions: steps.filter((s) => s.step_type === 'TOOL_EXECUTED').length,
    toolFailures: steps.filter((s) => s.step_type === 'TOOL_FAILED').length,
    modelTurns: steps.filter((s) => s.step_type === 'MODEL_RESPONSE').length,
    durationMs: runDurationMs(run),
    approvalWaitMs: between(requiredAt, resolvedAt),
  }
}

/** Compact human duration: 840ms, 4.2s, 1m 18s. */
export function formatDuration(value: number | null): string {
  if (value === null) return '—'
  if (value < 1000) return `${value}ms`

  const seconds = value / 1000

  if (seconds < 60) return `${seconds.toFixed(1)}s`

  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)

  return `${minutes}m ${rest}s`
}
