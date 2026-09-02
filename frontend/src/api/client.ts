export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

const UNREACHABLE =
  'Cannot reach the AgentGuard API. Check that the backend is running.'

const SERVER_ERROR = 'The AgentGuard API reported an internal error.'

/** An error safe to show a user: never carries a stack trace or server internals. */
export class ApiError extends Error {
  readonly status?: number

  constructor(message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function readDetail(response: Response): Promise<string | null> {
  try {
    const body = (await response.json()) as { detail?: unknown }

    return typeof body.detail === 'string' ? body.detail : null
  } catch {
    return null
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response

  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    // Network-level failure: the backend is down, or CORS rejected the call.
    throw new ApiError(UNREACHABLE)
  }

  if (!response.ok) {
    // 5xx bodies may carry internals, so only 4xx detail is surfaced.
    if (response.status >= 500) {
      throw new ApiError(SERVER_ERROR, response.status)
    }

    const detail = await readDetail(response)

    throw new ApiError(
      detail ?? `Request failed (${response.status}).`,
      response.status,
    )
  }

  return (await response.json()) as T
}

export function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') {
      search.set(key, String(value))
    }
  }

  const rendered = search.toString()

  return rendered ? `?${rendered}` : ''
}
