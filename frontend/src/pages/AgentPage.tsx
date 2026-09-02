import { useState } from 'react'
import { Link } from 'react-router-dom'
import { resolveApproval, runAgent } from '../api/agentguard'
import type { AgentResponse, ApprovalResponse } from '../api/types'
import { ApprovalCard } from '../components/ApprovalCard'
import { RunStatusBadge } from '../components/Badges'
import { PageHeader } from '../components/Layout'
import { ErrorState } from '../components/States'
import { TraceList } from '../components/TraceList'

const PLACEHOLDER = 'Investigate migration batch 43 and restart it if needed.'

interface RunView {
  runId: string
  status: AgentResponse['status']
  answer: string
  trace: AgentResponse['trace']
  approval: AgentResponse['approval_required']
}

function toView(
  response: AgentResponse | ApprovalResponse,
  previous?: RunView,
): RunView {
  const status = 'status' in response ? response.status : response.run_status

  // A resumed run returns only the steps executed after approval, so the trace
  // from before the pause is preserved rather than replaced.
  const trace =
    'run_status' in response
      ? [...(previous?.trace ?? []), ...response.trace]
      : response.trace

  return {
    runId: response.run_id,
    status,
    answer: response.answer,
    trace,
    approval: response.approval_required,
  }
}

export function AgentPage() {
  const [prompt, setPrompt] = useState('')
  const [submitted, setSubmitted] = useState<string | null>(null)
  const [view, setView] = useState<RunView | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()

    const message = prompt.trim()

    if (!message || busy) return

    setBusy(true)
    setError(null)
    setView(null)
    setSubmitted(message)

    try {
      setView(toView(await runAgent(message)))
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
      const response = await resolveApproval(view.approval.approval_id, approved)

      setView(toView(response, view))
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Agent"
        subtitle="Ask the agent to investigate or act. Every action it proposes is governed before it executes."
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

      {submitted && !error ? (
        <div className="stack">
          <div className="prompt-echo">{submitted}</div>

          {busy && !view ? (
            <div className="card row" role="status">
              <span className="spinner" />
              <span className="muted">Agent is reasoning…</span>
            </div>
          ) : null}

          {view ? (
            <>
              <div className="card">
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <div className="row">
                    <RunStatusBadge status={view.status} />
                    <span className="faint mono" style={{ fontSize: 12 }}>
                      {view.runId}
                    </span>
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
              ) : (
                <div>
                  <div className="approval-term">Answer</div>
                  <div className="answer">{view.answer}</div>
                </div>
              )}

              <div>
                <div className="approval-term" style={{ marginBottom: 8 }}>
                  Tool activity
                </div>
                <TraceList trace={view.trace} />
              </div>
            </>
          ) : null}
        </div>
      ) : null}
    </>
  )
}
