from app.tool_registry import Tool, ToolRegistry
from app.tools import calculator, get_migration_status


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