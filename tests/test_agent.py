from types import SimpleNamespace

from app.agent import AgentService
from app.model_provider import ModelProvider
from app.tool_registry import Tool, ToolRegistry
from app.tools import get_migration_status


class FakeModelProvider(ModelProvider):
    def __init__(self) -> None:
        self.call_count = 0

    def generate(self, message: str) -> str:
        return "Fake response"

    def generate_with_tools(
        self,
        input_items,
        tools,
    ):
        self.call_count += 1

        if self.call_count == 1:
            function_call = SimpleNamespace(
                type="function_call",
                name="get_migration_status",
                arguments='{"batch_id": 43}',
                call_id="test-call-123",
            )

            return SimpleNamespace(
                output=[function_call],
                output_text="",
            )

        return SimpleNamespace(
            output=[],
            output_text="Batch 43 failed because of an Oracle connection timeout.",
        )


def test_agent_executes_migration_tool():
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="get_migration_status",
            description="Get migration status.",
            function=get_migration_status,
            parameters={},
        )
    )

    model = FakeModelProvider()

    agent = AgentService(
        model=model,
        tool_registry=registry,
    )

    answer, trace = agent.run(
        "What happened to migration batch 43?"
    )

    assert answer == (
        "Batch 43 failed because of an Oracle connection timeout."
    )

    assert len(trace) == 1

    assert trace[0]["tool"] == "get_migration_status"

    assert trace[0]["arguments"]["batch_id"] == 43

    assert trace[0]["result"]["status"] == "FAILED"

    assert trace[0]["result"]["error"] == "Oracle connection timeout"

    assert model.call_count == 2