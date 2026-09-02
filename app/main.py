from fastapi import FastAPI

from app.agent import AgentService
from app.model_provider import OpenAIModelProvider
from app.models import AgentRequest, AgentResponse
from app.tool_registry import Tool, ToolRegistry
from app.tools import calculator, get_migration_status


app = FastAPI(title="Enterprise AgentOps")

model_provider = OpenAIModelProvider()

tool_registry = ToolRegistry()

tool_registry.register(
    Tool(
        name="calculator",
        description="Perform a basic arithmetic operation.",
        function=calculator,
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
                "operation": {
                    "type": "string",
                    "enum": [
                        "add",
                        "subtract",
                        "multiply",
                        "divide",
                    ],
                },
            },
            "required": [
                "a",
                "b",
                "operation",
            ],
            "additionalProperties": False,
        },
    )
)

tool_registry.register(
    Tool(
        name="get_migration_status",
        description=(
            "Get the actual migration status and "
            "error details for a specific batch ID."
        ),
        function=get_migration_status,
        parameters={
            "type": "object",
            "properties": {
                "batch_id": {"type": "integer"},
            },
            "required": ["batch_id"],
            "additionalProperties": False,
        },
    )
)

agent = AgentService(
    model=model_provider,
    tool_registry=tool_registry,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/run", response_model=AgentResponse)
def run_agent(request: AgentRequest) -> AgentResponse:
    answer, trace = agent.run(request.message)

    return AgentResponse(
        answer=answer,
        trace=trace,
    )