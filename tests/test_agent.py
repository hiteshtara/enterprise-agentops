from types import SimpleNamespace

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.model_provider import ModelProvider
from app.tool_registry import Tool, ToolRegistry, ToolRisk
from app.tools import get_migration_status, restart_migration


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
            output_text=("Batch 43 failed because of an Oracle connection timeout."),
        )


class FakeWriteModelProvider(ModelProvider):
    def generate(self, message: str) -> str:
        return "Fake response"

    def generate_with_tools(
        self,
        input_items,
        tools,
    ):
        function_call = SimpleNamespace(
            type="function_call",
            name="restart_migration",
            arguments='{"batch_id": 43}',
            call_id="write-call-123",
        )

        return SimpleNamespace(
            output=[function_call],
            output_text="",
        )


def test_agent_executes_migration_tool(database):
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
        approval_store=ApprovalStore(database=database),
        audit_store=AuditStore(database=database),
    )

    result = agent.run("What happened to migration batch 43?")

    assert result["answer"] == (
        "Batch 43 failed because of an Oracle connection timeout."
    )

    assert result["approval_required"] is None
    assert len(result["trace"]) == 1
    assert result["trace"][0]["tool"] == "get_migration_status"
    assert result["trace"][0]["arguments"]["batch_id"] == 43
    assert result["trace"][0]["result"]["status"] == "FAILED"
    assert result["trace"][0]["result"]["error"] == "Oracle connection timeout"

    assert model.call_count == 2


def test_agent_blocks_write_tool_without_approval(database):
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="restart_migration",
            description="Restart migration.",
            function=restart_migration,
            parameters={},
            risk=ToolRisk.WRITE,
        )
    )

    model = FakeWriteModelProvider()

    agent = AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=database),
        audit_store=AuditStore(database=database),
    )

    result = agent.run("Restart migration batch 43.")

    assert result["answer"] == ("Approval required before executing restart_migration.")

    assert result["trace"] == []

    approval = result["approval_required"]

    assert approval is not None
    assert approval["tool"] == "restart_migration"
    assert approval["arguments"]["batch_id"] == 43
    assert approval["risk"] == "WRITE"
    assert approval["approval_id"]
