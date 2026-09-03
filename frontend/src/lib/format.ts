// Formatting for measured values.
//
// The rule everywhere: a null is unknown, and unknown is never rendered as 0.

const UNAVAILABLE = 'Unavailable'

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return UNAVAILABLE
  if (value < 1000) return `${value} ms`

  const seconds = value / 1000

  if (seconds < 60) return `${seconds.toFixed(1)} s`

  const minutes = Math.floor(seconds / 60)

  return `${minutes}m ${Math.round(seconds % 60)}s`
}

export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return UNAVAILABLE

  return value.toLocaleString()
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return UNAVAILABLE

  return String(value)
}

/** Estimated USD. Unknown pricing shows "$—", never "$0.00". */
export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return '$—'
  if (value === 0) return '$0.00'
  if (value < 0.01) return `$${value.toFixed(6)}`

  return `$${value.toFixed(4)}`
}
