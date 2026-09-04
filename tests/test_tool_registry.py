import pytest

from app.tool_registry import (
    ApprovalRequired,
    Tool,
    ToolRegistry,
    ToolRisk,
)
from app.tools import (
    calculator,
    get_migration_status,
    restart_migration,
)


def test_calculator_tool():
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="calculator",
            description="Perform arithmetic.",
            function=calculator,
            parameters={},
        )
    )

    result = registry.execute(
        "calculator",
        {
            "a": 47,
            "b": 83,
            "operation": "add",
        },
    )

    assert result == 130


def test_migration_status_tool():
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="get_migration_status",
            description="Get migration status.",
            function=get_migration_status,
            parameters={},
        )
    )

    result = registry.execute(
        "get_migration_status",
        {
            "batch_id": 43,
        },
    )

    assert result["status"] == "FAILED"
    assert result["records"] == 495
    assert result["error"] == "Oracle connection timeout"


def test_write_tool_requires_approval():
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

    with pytest.raises(ApprovalRequired):
        registry.execute(
            "restart_migration",
            {
                "batch_id": 43,
            },
        )


def test_write_tool_executes_when_approved():
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

    result = registry.execute(
        "restart_migration",
        {
            "batch_id": 43,
        },
        approved=True,
    )

    assert result["status"] == "RESTARTED"


# -- registered, governed, and never advertised ----------------------------
#
# Some capabilities are for the application to invoke on a person's behalf, with
# text a person wrote and a human approval behind them. `model_callable=False`
# is how such a tool stays inside governance without being offered to the model:
# telling the model it exists would hand it the one action the design withholds,
# and not registering it would take the action outside governance altogether.


def hidden_tool(calls: list) -> Tool:
    return Tool(
        name="send_something_irreversible",
        description="Reaches a real person. Human-approved only.",
        function=lambda **arguments: calls.append(arguments) or "sent",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.DANGEROUS,
        model_callable=False,
    )


def test_a_non_model_callable_tool_is_absent_from_definitions():
    registry = ToolRegistry()
    calls: list = []

    registry.register(hidden_tool(calls))
    registry.register(
        Tool(
            name="calculator",
            description="Perform arithmetic.",
            function=calculator,
            parameters={},
        )
    )

    assert [definition.name for definition in registry.definitions()] == ["calculator"]


def test_a_non_model_callable_tool_is_still_described_for_the_console():
    registry = ToolRegistry()

    registry.register(hidden_tool([]))

    described = {tool["name"]: tool for tool in registry.describe()}

    assert described["send_something_irreversible"]["risk"] == "DANGEROUS"


def test_a_non_model_callable_tool_is_still_approval_gated():
    registry = ToolRegistry()
    calls: list = []

    registry.register(hidden_tool(calls))

    with pytest.raises(ApprovalRequired):
        registry.execute("send_something_irreversible", {})

    assert calls == []

    assert registry.execute("send_something_irreversible", {}, approved=True) == "sent"
    assert calls == [{}]


def test_tools_are_model_callable_by_default():
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="calculator",
            description="Perform arithmetic.",
            function=calculator,
            parameters={},
        )
    )

    assert registry.get("calculator").model_callable is True
