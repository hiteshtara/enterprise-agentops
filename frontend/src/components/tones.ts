// Kept out of Badges.tsx so that module exports only components, which is what
// React Fast Refresh requires.

export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'neutral'

/** Shared by audit event badges and run-step timeline nodes. */
export const EVENT_TONE: Record<string, Tone> = {
  TOOL_REQUESTED: 'neutral',
  TOOL_EXECUTED: 'ok',
  TOOL_FAILED: 'danger',
  APPROVAL_REQUIRED: 'warn',
  APPROVAL_GRANTED: 'ok',
  APPROVAL_DENIED: 'danger',
  AGENT_FAILED: 'danger',
  AGENT_MAX_ITERATIONS: 'danger',
  RUN_RECONCILED: 'danger',
  AUTHORIZATION_DENIED: 'danger',
  MODEL_RESPONSE: 'info',
}
