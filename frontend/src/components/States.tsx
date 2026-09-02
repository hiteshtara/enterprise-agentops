import { ApiError } from '../api/client'

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="state" role="status">
      <span className="spinner" /> {label}…
    </div>
  )
}

export function Empty({ message }: { message: string }) {
  return <div className="state">{message}</div>
}

/**
 * Renders a user-safe message. Anything that is not an ApiError is reported
 * generically -- an unexpected exception's text never reaches the screen.
 */
export function ErrorState({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? error.message
      : 'Something went wrong loading this view.'

  return (
    <div className="state state-error" role="alert">
      {message}
    </div>
  )
}
