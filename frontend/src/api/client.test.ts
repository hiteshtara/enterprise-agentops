import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from './client'
import { clearToken, setToken } from './session'

/**
 * The console declares which surface a request came from.
 *
 * After the 2026-09-07 incident — five pricing approvals in ninety seconds
 * that the audit trail could not attribute — every console request carries
 * `X-AgentGuard-Source: UI`, so an operator reading the audit log afterwards
 * can tell a click from a script.
 *
 * It is a declaration, not a credential: the backend authorizes nothing on
 * it, and it identifies this application rather than the person using it.
 */

function ok(body: unknown = {}) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

function headersOf(call: number = 0): Record<string, string> {
  const init = vi.mocked(globalThis.fetch).mock.calls[call][1] as RequestInit

  return init.headers as Record<string, string>
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => ok()),
  )
  clearToken()
})

afterEach(() => {
  vi.unstubAllGlobals()
  clearToken()
})

describe('the console declares its source', () => {
  it('sends UI on every request', async () => {
    await request('/runs')

    expect(headersOf()['X-AgentGuard-Source']).toBe('UI')
  })

  it('sends UI even when unauthenticated', async () => {
    await request('/auth/login', { method: 'POST', body: '{}' })

    expect(headersOf()['X-AgentGuard-Source']).toBe('UI')
  })

  it('sends UI alongside the bearer token', async () => {
    setToken('a-token')

    await request('/runs')

    const headers = headersOf()

    expect(headers['X-AgentGuard-Source']).toBe('UI')
    expect(headers.Authorization).toBe('Bearer a-token')
  })

  it('carries no network or device identifier', async () => {
    setToken('a-token')

    await request('/runs')

    const keys = Object.keys(headersOf()).map((k) => k.toLowerCase())

    // The console must not volunteer anything that identifies a machine or a
    // person. Absence is the property being tested.
    for (const forbidden of [
      'x-forwarded-for',
      'x-real-ip',
      'x-device-id',
      'x-fingerprint',
      'x-session-id',
    ]) {
      expect(keys).not.toContain(forbidden)
    }
  })

  it('lets a caller override the source without breaking the request', async () => {
    // `init.headers` is spread last, so a deliberate override wins. Nothing
    // depends on the value being UI — it is evidence, not enforcement.
    await request('/runs', { headers: { 'X-AgentGuard-Source': 'CLI' } })

    expect(headersOf()['X-AgentGuard-Source']).toBe('CLI')
  })
})
