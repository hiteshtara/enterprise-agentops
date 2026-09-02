from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.run_store import RunStatus, RunStore
from app.tool_registry import Tool, ToolRegistry, ToolRisk
from app.tools import get_migration_status, restart_migration
from tests.fakes import ScriptedModelProvider, final_response, tool_response


def build_registry(name, function, risk=ToolRisk.READ) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        Tool(
            name=name,
            description=f"{name} tool.",
            function=function,
            parameters={},
            risk=risk,
        )
    )

    return registry


def build_agent(database, registry, model) -> AgentService:
    return AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=database),
        audit_store=AuditStore(database=database),
        run_store=RunStore(database=database),
    )


def test_agent_executes_migration_tool(database):
    registry = build_registry("get_migration_status", get_migration_status)

    model = ScriptedModelProvider(
        [
            tool_response("get_migration_status", {"batch_id": 43}),
            final_response(
                "Batch 43 failed because of an Oracle connection timeout.",
            ),
        ]
    )

    result = build_agent(database, registry, model).run(
        "What happened to migration batch 43?",
    )

    assert result["answer"] == (
        "Batch 43 failed because of an Oracle connection timeout."
    )

    assert result["approval_required"] is None
    assert result["status"] == RunStatus.COMPLETED.value
    assert result["run_id"]

    assert len(result["trace"]) == 1
    assert result["trace"][0]["tool"] == "get_migration_status"
    assert result["trace"][0]["arguments"]["batch_id"] == 43
    assert result["trace"][0]["result"]["status"] == "FAILED"
    assert result["trace"][0]["result"]["error"] == "Oracle connection timeout"

    assert model.call_count == 2


def test_agent_blocks_write_tool_without_approval(database):
    registry = build_registry(
        "restart_migration",
        restart_migration,
        risk=ToolRisk.WRITE,
    )

    model = ScriptedModelProvider(
        [
            tool_response("restart_migration", {"batch_id": 43}),
            final_response("Never reached."),
        ]
    )

    result = build_agent(database, registry, model).run("Restart migration batch 43.")

    assert result["answer"] == ("Approval required before executing restart_migration.")

    assert result["trace"] == []
    assert result["status"] == RunStatus.WAITING_FOR_APPROVAL.value

    approval = result["approval_required"]

    assert approval is not None
    assert approval["tool"] == "restart_migration"
    assert approval["arguments"]["batch_id"] == 43
    assert approval["risk"] == "WRITE"
    assert approval["approval_id"]
    assert approval["run_id"] == result["run_id"]
