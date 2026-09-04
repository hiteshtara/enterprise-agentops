"""Builds the ToolRegistry the agent exposes to the model.

Kept out of main.py so that tool wiring can be constructed and inspected in
tests without importing the FastAPI app or a model provider.
"""

from app.connectors.lodgify.enquiry_tools import (
    SEND_ENQUIRY_REPLY_SCHEMA,
    LodgifyEnquiryTools,
)
from app.connectors.lodgify.messaging_tools import (
    GET_CONVERSATION_SCHEMA,
    LIST_CONVERSATIONS_SCHEMA,
    SEND_REPLY_SCHEMA,
    LodgifyMessagingTools,
)
from app.connectors.lodgify.tools import (
    AVAILABILITY_SCHEMA,
    LIST_PROPERTIES_SCHEMA,
    QUOTE_SCHEMA,
    LodgifyTools,
)
from app.connectors.pricelabs.pricing_tools import (
    APPLY_PRICING_ACTION_SCHEMA,
    APPLY_PRICING_ACTION_TOOL,
    PriceLabsPricingTools,
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


def lodgify_messaging_tools(tools: LodgifyMessagingTools) -> list[Tool]:
    """Guest conversations: two reads and the one governed send.

    `send_guest_reply` is DANGEROUS, not WRITE. It reaches a real person outside
    the business, cannot be edited or recalled once sent, and has no provider
    idempotency key -- so a duplicate is a real duplicate. WRITE would be
    defensible only if the action were confined to internal records.
    """
    return [
        Tool(
            name="list_recent_guest_conversations",
            description=(
                "List recent guest conversations for the managed properties, "
                "most recently active first. Each row says whether it appears "
                "to need attention: 'needs_attention' means the newest message "
                "is from the guest, 'responded' means the newest is ours, and "
                "'unknown' means it could not be determined -- treat unknown as "
                "unknown, not as needing a reply. Use the conversation_ref from "
                "here for every other conversation tool."
            ),
            function=tools.list_recent_guest_conversations,
            risk=ToolRisk.READ,
            parameters=LIST_CONVERSATIONS_SCHEMA,
        ),
        Tool(
            name="get_guest_conversation",
            description=(
                "Read one guest conversation in full, oldest message first, "
                "along with the Priyanka Homes reply guidance that governs what "
                "may be promised. Read this before drafting any reply, and "
                "follow the guidance it returns -- particularly its list of "
                "topics that must never be answered from memory."
            ),
            function=tools.get_guest_conversation,
            risk=ToolRisk.READ,
            parameters=GET_CONVERSATION_SCHEMA,
        ),
        Tool(
            name="send_guest_reply",
            description=(
                "Send a reply to a real guest. This is irreversible and "
                "externally visible: the guest receives it, and it cannot be "
                "edited or unsent. It requires human approval, and the text is "
                "sent exactly as written. "
                "If the result is 'unknown_send_state', the message may already "
                "have been delivered -- do NOT call this tool again for the "
                "same conversation. Report the uncertainty and let a person "
                "check the thread."
            ),
            function=tools.send_guest_reply,
            risk=ToolRisk.DANGEROUS,
            parameters=SEND_REPLY_SCHEMA,
        ),
    ]


def send_enquiry_reply_tool(tools: LodgifyEnquiryTools) -> Tool:
    """The enquiry send: DANGEROUS, and never advertised to the model.

    DANGEROUS for the same reason `send_guest_reply` is -- it reaches a real
    person outside the business, cannot be recalled, and has no provider
    idempotency key, so a duplicate is a real duplicate.

    `model_callable=False` is the second half. An enquiry reply exists because
    a person read a draft, edited it, and pressed a button; the words are the
    model's, the decision to transmit them is not. Advertising this tool would
    let a drafting run choose to send, which is precisely the authority this
    design withholds. It stays registered so it stays governed: the console
    lists it, the approval gate stands in front of it, and every request and
    execution is audited like any other.
    """
    return Tool(
        name="send_enquiry_reply",
        description=(
            "Send a reply to a real person who enquired about a property. This "
            "is irreversible and externally visible: they receive it, and it "
            "cannot be edited or unsent. It requires human approval, and the "
            "text is sent exactly as written. If the result is "
            "'unknown_send_state', the message may already have been delivered "
            "-- it must not be sent again for the same enquiry; a person has to "
            "check the thread."
        ),
        function=tools.send_enquiry_reply,
        risk=ToolRisk.DANGEROUS,
        model_callable=False,
        parameters=SEND_ENQUIRY_REPLY_SCHEMA,
    )


def apply_pricing_action_tool(pricing: PriceLabsPricingTools) -> Tool:
    """The pricing write, registered but never advertised to the model.

    `model_callable=False` is the whole point: the capability stays inside
    governance -- risk-tiered, approval-gated, audited, visible to the console
    -- while remaining absent from what the provider is told the model can do.
    """
    return Tool(
        name=APPLY_PRICING_ACTION_TOOL,
        description=(
            "Apply one owner-approved pricing action to PriceLabs. Refuses if "
            "the state changed since the recommendation was made."
        ),
        function=pricing.apply_pricing_action,
        risk=ToolRisk.DANGEROUS,
        model_callable=False,
        parameters=APPLY_PRICING_ACTION_SCHEMA,
    )


def build_tool_registry(
    migration_store: MigrationBatchStore,
    lodgify: LodgifyTools | None = None,
    lodgify_messaging: LodgifyMessagingTools | None = None,
    lodgify_enquiries: LodgifyEnquiryTools | None = None,
    pricelabs_pricing: PriceLabsPricingTools | None = None,
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

    if pricelabs_pricing is not None:
        registry.register(apply_pricing_action_tool(pricelabs_pricing))

    registry.register(calculator_tool())
    registry.register(get_migration_status_tool())
    registry.register(restart_migration_tool())
    registry.register(query_migration_batches_tool(migration_store))

    if lodgify is not None:
        for tool in lodgify_tools(lodgify):
            registry.register(tool)

    if lodgify_messaging is not None:
        for tool in lodgify_messaging_tools(lodgify_messaging):
            registry.register(tool)

    if lodgify_enquiries is not None:
        registry.register(send_enquiry_reply_tool(lodgify_enquiries))

    return registry
