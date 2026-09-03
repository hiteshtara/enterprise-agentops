import { useState } from 'react'
import { reconcileRuns } from '../api/agentguard'
import { useAuth } from '../auth/context'
import { ApiError } from '../api/client'

const DEFAULT_STALE_SECONDS = 900

const THRESHOLDS = [
  { label: '15 minutes', value: 900 },
  { label: '1 hour', value: 3600 },
  { label: '6 hours', value: 21600 },
  { label: '24 hours', value: 86400 },
]

/**
 * Admin-only recovery for runs abandoned by a crashed process.
 *
 * The console offers the action only to a role that holds RECONCILE_RUNS, but
 * the backend re-checks and is the only authority.
 */
export function ReconcileAction({ onReconciled }: { onReconciled: () => void }) {
  const { can } = useAuth()

  const [confirming, setConfirming] = useState(false)
  const [seconds, setSeconds] = useState(DEFAULT_STALE_SECONDS)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)

  if (!can('RECONCILE_RUNS')) return null

  async function run() {
    setBusy(true)
    setError(null)
    setResult(null)

    try {
      const response = await reconcileRuns(seconds)

      setResult(
        response.count === 0
          ? 'No stale runs found. Nothing was changed.'
          : `Marked ${response.count} interrupted run${response.count > 1 ? 's' : ''} as FAILED.`,
      )

      setConfirming(false)
      onReconciled()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  const message =
    error instanceof ApiError
      ? error.message
      : error
        ? 'Reconciliation could not be completed.'
        : null

  return (
    <div className="reconcile">
      {!confirming ? (
        <button type="button" onClick={() => setConfirming(true)}>
          Reconcile stale runs
        </button>
      ) : (
        <div
          className="card reconcile-panel"
          role="dialog"
          aria-label="Confirm reconciliation"
          aria-modal="false"
        >
          <p style={{ margin: '0 0 12px' }}>
            RUNNING runs older than the selected threshold will be marked FAILED as
            interrupted. Runs waiting for approval and finished runs are never affected.
          </p>

          <div className="field" style={{ marginBottom: 12 }}>
            <label className="field-label" htmlFor="stale-threshold">
              Stale after
            </label>
            <select
              id="stale-threshold"
              value={seconds}
              disabled={busy}
              onChange={(event) => setSeconds(Number(event.target.value))}
            >
              {THRESHOLDS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className="row">
            <button type="button" className="primary" disabled={busy} onClick={run}>
              {busy ? 'Reconciling…' : 'Reconcile'}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirming(false)
                setError(null)
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {result ? (
        <p className="reconcile-result" role="status">
          {result}
        </p>
      ) : null}

      {message ? (
        <p className="state state-error" role="alert" style={{ marginTop: 10 }}>
          {message}
        </p>
      ) : null}
    </div>
  )
}
