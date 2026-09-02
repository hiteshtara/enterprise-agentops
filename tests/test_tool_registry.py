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
