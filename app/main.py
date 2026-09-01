from fastapi import FastAPI

from app.agent import AgentService
from app.model_provider import OpenAIModelProvider
from app.models import AgentRequest, AgentResponse


app = FastAPI(title="Enterprise AgentOps")

model_provider = OpenAIModelProvider()
agent = AgentService(model_provider)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/run", response_model=AgentResponse)
def run_agent(request: AgentRequest) -> AgentResponse:
    answer = agent.run(request.message)

    return AgentResponse(answer=answer)