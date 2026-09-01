from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    message: str = Field(min_length=1)


class AgentResponse(BaseModel):
    answer: str