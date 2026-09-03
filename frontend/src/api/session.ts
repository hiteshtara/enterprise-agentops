// Where the bearer token lives for this V1.
//
// sessionStorage is deliberate: the credential is scoped to the tab and is
// dropped when it closes, and it is never written to a cookie, so there is no
// ambient credential for a cross-site request to ride on.

const TOKEN_KEY = 'agentguard.token'

let cached: string | null = null

function storage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    // Private mode or a blocked-storage policy: fall back to memory only.
    return null
  }
}

export function getToken(): string | null {
  if (cached !== null) return cached

  try {
    cached = storage()?.getItem(TOKEN_KEY) ?? null
  } catch {
    cached = null
  }

  return cached
}

export function setToken(token: string | null): void {
  cached = token

  try {
    if (token === null) storage()?.removeItem(TOKEN_KEY)
    else storage()?.setItem(TOKEN_KEY, token)
  } catch {
    // Memory-only is an acceptable degradation; the session ends on reload.
  }
}

export function clearToken(): void {
  setToken(null)
}
