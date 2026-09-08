import { useState } from 'react'
import {
  endPricingSession,
  getPricingSession,
  startPricingSession,
} from '../api/agentguard'
import type { PricingBandsOut, PricingSession } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { ApiError } from '../api/client'

/**
 * The owner's deliberate pricing window.
 *
 * **Opening one changes no price.** It narrows what the deployment already
 * permits, and every individual change still stops for its own approval. The
 * confirmation text says exactly that, before anything is opened.
 *
 * Two rules the UI has to keep, both learned the hard way:
 *
 *   * **Never offer a write path the deployment forbids.** The payload carries
 *     the deployment kill switch and allowlist alongside the session, so a
 *     window opened while writes are off is shown honestly as functional-test
 *     only rather than dressed up as operable. Offering it anyway is what
 *     produced doomed approvals and a red failure box for a switch that was
 *     simply off.
 *   * **No select-all.** Choosing the whole portfolio should be something
 *     someone did deliberately, not a default they accepted, so the shortcut
 *     that makes it one click does not exist.
 */

/** How often the countdown refreshes. Also how quickly an expiry is noticed. */
const POLL_MS = 15_000

function minutes(seconds: number): number {
  return Math.max(0, Math.ceil(seconds / 60))
}

export function PricingSessionBadge() {
  const session = useAsync(getPricingSession, [], { intervalMs: POLL_MS })

  if (!session.data) return null

  const live = session.data.mode === 'LIVE'

  return (
    <div className={`pricing-status ${live ? 'tone-warn' : 'tone-neutral'}`}>
      <span className="badge-dot" aria-hidden="true" />
      <strong>Pricing</strong>{' '}
      {live ? (
        <>
          <span>LIVE — {minutes(session.data.remaining_seconds)} min</span>
          {session.data.listing_ids.length ? (
            <div className="faint">{session.data.listing_ids.join(', ')}</div>
          ) : null}
        </>
      ) : (
        <span>SAFE MODE</span>
      )}
    </div>
  )
}

export function PricingSessionControl({
  bands,
  canAdminister,
}: {
  bands: PricingBandsOut[]
  canAdminister: boolean
}) {
  const session = useAsync(getPricingSession, [], { intervalMs: POLL_MS })

  const [opening, setOpening] = useState(false)
  const [chosen, setChosen] = useState<string[]>([])
  const [duration, setDuration] = useState(30)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const data: PricingSession | null = session.data ?? null

  if (!data) return null

  const live = data.mode === 'LIVE'
  const deploymentOff = !data.deployment_writes_enabled

  function toggle(listingId: string) {
    setChosen((current) =>
      current.includes(listingId)
        ? current.filter((id) => id !== listingId)
        : [...current, listingId],
    )
  }

  async function start() {
    setBusy(true)
    setError(null)

    try {
      await startPricingSession(chosen, duration)
      setOpening(false)
      setChosen([])
      session.reload()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not start.')
    } finally {
      setBusy(false)
    }
  }

  async function end() {
    setBusy(true)
    setError(null)

    try {
      await endPricingSession()
      session.reload()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not end.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card pricing-session">
      <div className="row" style={{ gap: 8 }}>
        <strong>{live ? 'LIVE PRICING ON' : 'LIVE PRICING: OFF'}</strong>
        {live ? (
          <span>
            {minutes(data.remaining_seconds)} minutes remaining · Enabled listings:{' '}
            {data.listing_ids.join(', ')}
          </span>
        ) : null}
      </div>

      {deploymentOff ? (
        <div className="state state-warn" role="note">
          <strong>DEPLOYMENT PRICING IS DISABLED</strong>
          <div>
            This installation is not permitted to send pricing changes, so no change can
            be applied even during a session. A session may still be opened to exercise
            the controls.
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="state state-error" role="alert">
          {error}
        </div>
      ) : null}

      {!canAdminister ? (
        <p className="faint">Only an administrator can start a pricing session.</p>
      ) : live ? (
        <button type="button" disabled={busy} onClick={end}>
          End pricing session
        </button>
      ) : opening ? (
        <div className="session-form">
          <p className="callout tone-warn">
            During this session, individually approved pricing changes can be sent to
            PriceLabs. Every change still requires its own human approval.
          </p>

          <fieldset>
            <legend>Listings ({chosen.length} selected)</legend>
            {bands.map((band) => (
              <label key={band.listing_id} className="row" style={{ gap: 6 }}>
                <input
                  type="checkbox"
                  checked={chosen.includes(band.listing_id)}
                  onChange={() => toggle(band.listing_id)}
                />
                {band.display_name}
              </label>
            ))}
          </fieldset>

          <label>
            Duration (minutes, max {data.max_session_minutes}){' '}
            <input
              type="number"
              min={1}
              max={data.max_session_minutes}
              value={duration}
              onChange={(e) => setDuration(Number(e.target.value))}
            />
          </label>

          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="primary"
              disabled={busy || chosen.length === 0}
              onClick={start}
            >
              Start pricing session
            </button>
            <button type="button" disabled={busy} onClick={() => setOpening(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button type="button" onClick={() => setOpening(true)}>
          Start pricing session
        </button>
      )}
    </div>
  )
}
