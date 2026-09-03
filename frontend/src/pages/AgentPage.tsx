import { useCallback, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { getRun, listApprovals, resolveApproval, runAgent } from '../api/agentguard'
import type { RunStatus } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { viewFromResponse, viewFromRun, type RunView } from '../lib/runView'
import { ApprovalCard } from '../components/ApprovalCard'
import { RunStatusBadge } from '../components/Badges'
import { PageHeader } from '../components/Layout'
import { ErrorState } from '../components/States'
import { TraceList } from '../components/TraceList'

const PLACEHOLDER = 'Investigate migration batch 43 and restart it if needed.'

const POLL_MS = 3000

const ACTIVE = new Set<RunStatus>(['RUNNING', 'WAITING_FOR_APPROVAL'])

/** Loads a run plus its pending approval, so the card can be rebuilt. */
async function loadRunView(runId: string): Promise<RunView> {
  const run = await getRun(runId)

  const pending =
    run.status === 'WAITING_FOR_APPROVAL'
      ? await listApprovals({ runId, status: 'PENDING', limit: 1 })
      : []

  return viewFromRun(run, pending[0] ?? null)
}

export function AgentPage() {
  const [params, setParams] = useSearchParams()

  const runParam = params.get('run') ?? ''

  const [prompt, setPrompt] = useState('')
  const [submitted, setSubmitted] = useState<RunView | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  // With ?run= in the URL the page rebuilds itself from durable state, so
  // navigating away and back -- or reloading, or opening the link fresh --
  // restores the run. Polling keeps an active run current.
  const restored = useAsync<RunView | null>(
    useCallback(
      () => (runParam ? loadRunView(runParam) : Promise.resolve(null)),
      [runParam],
    ),
    [runParam],
    {
      intervalMs: POLL_MS,
      pollWhile: (view) => view !== null && ACTIVE.has(view.status),
    },
  )

  // The just-submitted response wins until the URL-driven load catches up.
  const view =
    submitted && submitted.runId === runParam ? submitted : (restored.data ?? null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()

    const message = prompt.trim()

    if (!message || busy) return

    setBusy(true)
    setError(null)
    setSubmitted(null)

    try {
      const response = await runAgent(message)
      const next = viewFromResponse(message, response)

      setSubmitted(next)
      setParams({ run: next.runId }, { replace: false })
      setPrompt('')
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  async function decide(approved: boolean) {
    if (!view?.approval) return

    setBusy(true)
    setError(null)

    try {
      await resolveApproval(view.approval.approval_id, approved)

      // Re-read from the backend rather than splicing the response, so the
      // page shows exactly what was persisted.
      setSubmitted(null)
      restored.reload()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  function startNew() {
    setSubmitted(null)
    setError(null)
    setParams({}, { replace: false })
  }

  return (
    <>
      <PageHeader
        title="Agent"
        subtitle="Ask the agent to investigate or act. Every action it proposes is governed before it executes."
        actions={
          runParam ? (
            <button type="button" onClick={startNew} aria-label="Start a new request">
              New request
            </button>
          ) : null
        }
      />

      <form onSubmit={submit} className="card" style={{ marginBottom: 18 }}>
        <label className="field-label" htmlFor="prompt">
          Request
        </label>
        <textarea
          id="prompt"
          value={prompt}
          placeholder={PLACEHOLDER}
          onChange={(event) => setPrompt(event.target.value)}
          disabled={busy}
        />
        <div className="row" style={{ marginTop: 12, justifyContent: 'space-between' }}>
          <span className="faint" style={{ fontSize: 12 }}>
            READ tools run immediately. WRITE and DANGEROUS tools require approval.
          </span>
          <button type="submit" className="primary" disabled={busy || !prompt.trim()}>
            {busy && !view ? 'Running…' : 'Run agent'}
          </button>
        </div>
      </form>

      {error ? <ErrorState error={error} /> : null}
      {restored.error && !view ? <ErrorState error={restored.error} /> : null}

      {busy && !view ? (
        <div className="card row" role="status">
          <span className="spinner" />
          <span className="muted">Agent is reasoning…</span>
        </div>
      ) : null}

      {restored.loading && runParam && !view ? (
        <div className="card row" role="status">
          <span className="spinner" />
          <span className="muted">Restoring run…</span>
        </div>
      ) : null}

      {view ? (
        <div className="stack">
          <div className="prompt-echo">{view.prompt}</div>

          <div className="card">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <div className="row">
                <RunStatusBadge status={view.status} />
                <span className="faint mono" style={{ fontSize: 12 }}>
                  {view.runId}
                </span>
                {ACTIVE.has(view.status) ? (
                  <span className="faint" style={{ fontSize: 12 }}>
                    live — updating automatically
                  </span>
                ) : null}
              </div>
              <Link className="link" to={`/runs/${view.runId}`}>
                Open run detail →
              </Link>
            </div>
          </div>

          {view.approval ? (
            <ApprovalCard
              tool={view.approval.tool}
              risk={view.approval.risk}
              args={view.approval.arguments}
              runId={view.approval.run_id}
              busy={busy}
              onDecision={decide}
              evidence={
                view.trace.length > 0 ? (
                  <div className="muted" style={{ fontSize: 13 }}>
                    Based on {view.trace.length} authoritative tool result
                    {view.trace.length > 1 ? 's' : ''} gathered in this run.
                  </div>
                ) : null
              }
            />
          ) : view.answer ? (
            <div>
              <div className="approval-term">
                {view.status === 'FAILED' ? 'Failure reason' : 'Answer'}
              </div>
              <div
                className={view.status === 'FAILED' ? 'answer answer-failed' : 'answer'}
              >
                {view.answer}
              </div>
            </div>
          ) : null}

          <div>
            <div className="approval-term" style={{ marginBottom: 8 }}>
              Tool activity
            </div>
            <TraceList trace={view.trace} />
          </div>
        </div>
      ) : null}
    </>
  )
}
