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
    tool: str
    arguments: dict[str, Any]
    risk: str


class AgentResponse(BaseModel):
    answer: str
    trace: list[ToolTrace]
    approval_required: ApprovalRequest | None = None


class ApprovalDecision(BaseModel):
    approved: bool


class ApprovalResponse(BaseModel):
    approval_id: str
    approved: bool
    tool: str
    result: Any | None = None


class AuditEvent(BaseModel):
    id: int
    event_type: str
    details: dict[str, Any]
    created_at: str
