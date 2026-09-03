"""Builds the ToolRegistry the agent exposes to the model.

Kept out of main.py so that tool wiring can be constructed and inspected in
tests without importing the FastAPI app or a model provider.
"""

from app.connectors.lodgify.tools import (
    AVAILABILITY_SCHEMA,
    LIST_PROPERTIES_SCHEMA,
    QUOTE_SCHEMA,
    LodgifyTools,
)
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


def lodgify_tools(tools: LodgifyTools) -> list[Tool]:
    """The Lodgify connector's read-only capabilities.

    All three are READ: none of them creates, changes or cancels anything, and
    the connector has no write method to call even if one were registered.
    """
    return [
        Tool(
            name="list_properties",
            description=(
                "List the rental properties under management, and whether each "
                "one is bookable through Lodgify. Use this to discover the "
                "property slugs that the availability and quote tools accept."
            ),
            function=tools.list_properties,
            risk=ToolRisk.READ,
            parameters=LIST_PROPERTIES_SCHEMA,
        ),
        Tool(
            name="get_property_availability",
            description=(
                "Check live availability for one property over a date range, "
                "from the booking provider. Returns periods that are each "
                "available or not. If availability cannot be confirmed the "
                "result says so explicitly -- never assume a property is "
                "available when the result is unknown."
            ),
            function=tools.get_property_availability,
            risk=ToolRisk.READ,
            parameters=AVAILABILITY_SCHEMA,
        ),
        Tool(
            name="get_property_quote",
            description=(
                "Get authoritative pricing for one property, date range and "
                "guest count from the booking provider: accommodation, cleaning "
                "fee, taxes and total. Never calculate or estimate a price "
                "yourself; every figure quoted to a guest must come from this "
                "tool."
            ),
            function=tools.get_property_quote,
            risk=ToolRisk.READ,
            parameters=QUOTE_SCHEMA,
        ),
    ]


def build_tool_registry(
    migration_store: MigrationBatchStore,
    lodgify: LodgifyTools | None = None,
) -> ToolRegistry:
    """Assemble every tool the agent may call.

    Dependencies are passed in rather than constructed here, so callers control
    which database the tools read from.

    The Lodgify connector is optional. When it is not configured its tools are
    omitted entirely rather than registered in a broken state: the registry is
    what the model is told it can do, so advertising a capability that always
    fails wastes a reasoning iteration and invites the model to promise
    something it cannot deliver. AgentGuard runs fully without it.
    """
    registry = ToolRegistry()

    registry.register(calculator_tool())
    registry.register(get_migration_status_tool())
    registry.register(restart_migration_tool())
    registry.register(query_migration_batches_tool(migration_store))

    if lodgify is not None:
        for tool in lodgify_tools(lodgify):
            registry.register(tool)

    return registry
