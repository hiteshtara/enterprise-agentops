from typing import Any

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    message: str = Field(min_length=1)


class ToolTrace(BaseModel):
    tool: str
    arguments: dict[str, Any]
    result: Any


class ApprovalRequest(BaseModel):
    approval_id: str
    run_id: str
    tool: str
    arguments: dict[str, Any]
    risk: str


class AgentResponse(BaseModel):
    run_id: str
    status: str
    answer: str
    trace: list[ToolTrace]
    approval_required: ApprovalRequest | None = None


class ApprovalDecision(BaseModel):
    approved: bool


class ApprovalResponse(BaseModel):
    """Resolving an approval now resumes the run, so the resumed run's outcome
    travels with the decision. The original four fields are unchanged."""

    approval_id: str
    approved: bool
    tool: str
    result: Any | None = None
    run_id: str
    run_status: str
    answer: str
    trace: list[ToolTrace] = []
    approval_required: ApprovalRequest | None = None


class AuditEvent(BaseModel):
    id: int
    run_id: str | None = None
    event_type: str
    details: dict[str, Any]
    created_at: str


class RunStep(BaseModel):
    step_number: int
    step_type: str
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any | None = None
    error: dict[str, Any] | None = None
    created_at: str


class RunSummary(BaseModel):
    run_id: str
    status: str
    user_message: str
    final_answer: str | None = None
    created_at: str
    updated_at: str


class RunDetail(RunSummary):
    steps: list[RunStep] = []


class ApprovalSummary(BaseModel):
    approval_id: str
    run_id: str
    tool: str
    arguments: dict[str, Any]
    risk: str
    status: str
    created_at: str
    resolved_at: str | None = None
    decision: str | None = None


class ReconciledRun(BaseModel):
    run_id: str
    previous_status: str
    status: str
    reason: str


class ReconcileResponse(BaseModel):
    reconciled: list[ReconciledRun] = []
    count: int
