import { describe, expect, it } from 'vitest'
import { formatCost, formatCount, formatMs, formatTokens } from './format'

describe('formatting measured values', () => {
  it('renders durations at ms, second and minute scales', () => {
    expect(formatMs(12)).toBe('12 ms')
    expect(formatMs(4900)).toBe('4.9 s')
    expect(formatMs(78_000)).toBe('1m 18s')
  })

  it('never renders an unknown value as zero', () => {
    expect(formatMs(null)).toBe('Unavailable')
    expect(formatTokens(null)).toBe('Unavailable')
    expect(formatCount(null)).toBe('Unavailable')
    expect(formatCost(null)).toBe('$—')
  })

  it('distinguishes a genuine zero from an unknown', () => {
    expect(formatMs(0)).toBe('0 ms')
    expect(formatTokens(0)).toBe('0')
    expect(formatCost(0)).toBe('$0.00')
  })

  it('groups large token counts', () => {
    expect(formatTokens(3600)).toBe('3,600')
  })

  it('keeps small costs legible', () => {
    expect(formatCost(0.00174)).toBe('$0.001740')
    expect(formatCost(1.5)).toBe('$1.5000')
  })
})
