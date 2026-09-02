from typing import Any

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    message: str = Field(min_length=1)


class ToolTrace(BaseModel):
    tool: str
    arguments: dict[str, Any]
    result: Any


class AgentResponse(BaseModel):
    answer: str
    trace: list[ToolTrace]
