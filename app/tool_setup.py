"""Builds the ToolRegistry the agent exposes to the model.

Kept out of main.py so that tool wiring can be constructed and inspected in
tests without importing the FastAPI app or a model provider.
"""

from app.migration_store import (
    ALLOWED_STATUSES,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    MigrationBatchStore,
)
from app.tool_registry import Tool, ToolRegistry, ToolRisk
from app.tools import calculator, get_migration_status, restart_migration


def calculator_tool() -> Tool:
    return Tool(
        name="calculator",
        description=("Perform a basic arithmetic operation."),
        function=calculator,
        risk=ToolRisk.READ,
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


def get_migration_status_tool() -> Tool:
    return Tool(
        name="get_migration_status",
        description=(
            "Get the actual migration status and error details for a specific batch ID."
        ),
        function=get_migration_status,
        risk=ToolRisk.READ,
        parameters={
            "type": "object",
            "properties": {
                "batch_id": {"type": "integer"},
            },
            "required": ["batch_id"],
            "additionalProperties": False,
        },
    )


def restart_migration_tool() -> Tool:
    return Tool(
        name="restart_migration",
        description=("Restart a failed migration batch."),
        function=restart_migration,
        risk=ToolRisk.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "batch_id": {"type": "integer"},
            },
            "required": ["batch_id"],
            "additionalProperties": False,
        },
    )


def query_migration_batches_tool(
    migration_store: MigrationBatchStore,
) -> Tool:
    """Read-only query over authoritative migration batch records.

    The model chooses only typed, constrained arguments. The SQLAlchemy query
    itself is built inside MigrationBatchStore.query.
    """
    return Tool(
        name="query_migration_batches",
        description=(
            "Query the authoritative migration batch database. Returns real "
            "migration batch records, newest first, optionally filtered by "
            "status. Use this instead of guessing or recalling batch outcomes; "
            "every returned record is a real row from the migrations database."
        ),
        function=migration_store.query,
        risk=ToolRisk.READ,
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(ALLOWED_STATUSES),
                    "description": (
                        "Optional status filter. Omit to return batches of any status."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": MIN_LIMIT,
                    "maximum": MAX_LIMIT,
                    "description": (
                        f"Maximum number of batches to return. "
                        f"Defaults to {DEFAULT_LIMIT}."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )


def build_tool_registry(
    migration_store: MigrationBatchStore,
) -> ToolRegistry:
    """Assemble every tool the agent may call.

    Dependencies are passed in rather than constructed here, so callers control
    which database the tools read from.
    """
    registry = ToolRegistry()

    registry.register(calculator_tool())
    registry.register(get_migration_status_tool())
    registry.register(restart_migration_tool())
    registry.register(query_migration_batches_tool(migration_store))

    return registry
