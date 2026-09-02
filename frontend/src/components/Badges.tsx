import type {
  ApprovalStatus,
  EventType,
  RunStatus,
  StepType,
  ToolRisk,
} from '../api/types'
import { EVENT_TONE, type Tone } from './tones'

const RUN_TONE: Record<RunStatus, Tone> = {
  RUNNING: 'info',
  WAITING_FOR_APPROVAL: 'warn',
  COMPLETED: 'ok',
  FAILED: 'danger',
  CANCELLED: 'neutral',
}

const APPROVAL_TONE: Record<ApprovalStatus, Tone> = {
  PENDING: 'warn',
  APPROVED: 'ok',
  REJECTED: 'danger',
}

const RISK_TONE: Record<ToolRisk, Tone> = {
  READ: 'info',
  WRITE: 'warn',
  DANGEROUS: 'danger',
}

function label(value: string): string {
  return value.replace(/_/g, ' ')
}

export function Badge({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return (
    <span className={`badge tone-${tone}`}>
      <span className="badge-dot" aria-hidden="true" />
      {children}
    </span>
  )
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return <Badge tone={RUN_TONE[status] ?? 'neutral'}>{label(status)}</Badge>
}

export function ApprovalStatusBadge({ status }: { status: ApprovalStatus }) {
  return <Badge tone={APPROVAL_TONE[status] ?? 'neutral'}>{status}</Badge>
}

export function RiskBadge({ risk }: { risk: ToolRisk }) {
  return <Badge tone={RISK_TONE[risk] ?? 'neutral'}>{risk}</Badge>
}

export function EventBadge({ type }: { type: EventType | StepType }) {
  return <Badge tone={EVENT_TONE[type] ?? 'neutral'}>{label(type)}</Badge>
}
