// Rebuilds the Agent page's view of a run from durable backend state.
//
// The browser stores only a run_id (in the URL). Everything shown is derived
// from GET /runs/{id} and the run's pending approval, so a reload, a deep link
// and a fresh tab all reconstruct the same view. No provider or model internals
// are persisted client-side.

import type {
  AgentResponse,
  ApprovalRequest,
  ApprovalSummary,
  RunDetail,
  RunStatus,
  ToolTrace,
} from '../api/types'

export interface RunView {
  runId: string
  status: RunStatus
  prompt: string
  answer: string
  trace: ToolTrace[]
  approval: ApprovalRequest | null
}

/** Successful tool executions, in order, read off the persisted steps. */
export function traceFromSteps(run: RunDetail): ToolTrace[] {
  return run.steps
    .filter((step) => step.step_type === 'TOOL_EXECUTED' && step.tool_name)
    .map((step) => ({
      tool: step.tool_name as string,
      arguments: step.arguments ?? {},
      result: step.result,
    }))
}

export function viewFromRun(run: RunDetail, pending: ApprovalSummary | null): RunView {
  return {
    runId: run.run_id,
    status: run.status,
    prompt: run.user_message,
    answer: run.final_answer ?? '',
    trace: traceFromSteps(run),
    approval:
      pending && run.status === 'WAITING_FOR_APPROVAL'
        ? {
            approval_id: pending.approval_id,
            run_id: pending.run_id,
            requested_by_user_id: pending.requested_by_user_id,
            tool: pending.tool,
            arguments: pending.arguments,
            risk: pending.risk,
          }
        : null,
  }
}

/** The view straight after POST /agent/run, before anything is re-fetched. */
export function viewFromResponse(prompt: string, response: AgentResponse): RunView {
  return {
    runId: response.run_id,
    status: response.status,
    prompt,
    answer: response.answer,
    trace: response.trace,
    approval: response.approval_required,
  }
}
