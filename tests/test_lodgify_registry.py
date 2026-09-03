"""How the Lodgify connector plugs into the registry, agent, audit and metrics."""

import json

import httpx
import pytest

from app.connectors.lodgify.client import LodgifyClient
from app.connectors.lodgify.config import LODGIFY_SLUGS, MAX_GUESTS, MIN_GUESTS
from app.connectors.lodgify.tools import LodgifyTools
from app.observability_store import RunMetricsService
from app.tool_registry import ToolRisk
from app.tool_setup import build_tool_registry
from tests.fakes import ScriptedModelProvider, fake_usage, final_response, tool_response

FAKE_KEY = "test-only-not-a-real-lodgify-key"

SLUG = "boston-condo-second-floor"

LODGIFY_TOOL_NAMES = {
    "list_properties",
    "get_property_availability",
    "get_property_quote",
}

# Includes upstream fields that must never reach a trace, audit row or step.
AVAILABILITY_PAYLOAD = [
    {
        "property_id": 681293,
        "periods": [
            {
                "start": "2026-09-12",
                "end": "2026-09-14",
                "available": 1,
                "bookings": [{"id": 22578370, "guest_name": "A Real Guest"}],
                "channel_calendars": [{"channel": "Airbnb"}],
            }
        ],
    }
]


def lodgify_tools(payload=AVAILABILITY_PAYLOAD, status: int = 200) -> LodgifyTools:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return LodgifyTools(
        LodgifyClient(
            api_key_provider=lambda: FAKE_KEY,
            transport=httpx.MockTransport(handler),
        )
    )


@pytest.fixture
def connected_registry(migration_store):
    return build_tool_registry(
        migration_store=migration_store,
        lodgify=lodgify_tools(),
    )


# -- registration ----------------------------------------------------------


def test_the_registry_works_without_the_connector(registry):
    """AgentGuard's own demo must not depend on Lodgify."""
    names = {definition.name for definition in registry.definitions()}

    assert names == {
        "calculator",
        "get_migration_status",
        "restart_migration",
        "query_migration_batches",
    }
    assert not (names & LODGIFY_TOOL_NAMES)


def test_the_connector_adds_exactly_three_tools(connected_registry):
    names = {definition.name for definition in connected_registry.definitions()}

    assert LODGIFY_TOOL_NAMES <= names
    assert len(names) == 7


def test_existing_tools_are_unaffected_by_the_connector(connected_registry):
    assert connected_registry.get("restart_migration").risk is ToolRisk.WRITE
    assert connected_registry.get("query_migration_batches").risk is ToolRisk.READ


def test_every_lodgify_tool_is_read(connected_registry):
    for name in LODGIFY_TOOL_NAMES:
        assert connected_registry.get(name).risk is ToolRisk.READ


def test_no_lodgify_tool_requires_approval(connected_registry):
    """READ means it runs without a human in the loop -- so none may write."""
    result = connected_registry.execute("list_properties", {})

    assert isinstance(result, list)


# -- schemas ---------------------------------------------------------------


def describe(registry, name: str) -> dict:
    return next(t for t in registry.describe() if t["name"] == name)["parameters"]


def test_the_slug_argument_is_a_closed_enum(connected_registry):
    for name in ("get_property_availability", "get_property_quote"):
        schema = describe(connected_registry, name)

        slug = schema["properties"]["property_slug"]

        assert slug["type"] == "string"
        assert slug["enum"] == list(LODGIFY_SLUGS)
        assert schema["additionalProperties"] is False


def test_schemas_expose_no_provider_identifier_argument(connected_registry):
    for name in LODGIFY_TOOL_NAMES:
        properties = describe(connected_registry, name)["properties"]

        for forbidden in (
            "property_id",
            "lodgify_property_id",
            "room_type_id",
            "roomTypeId",
            "api_key",
            "url",
        ):
            assert forbidden not in properties, f"{name} exposes {forbidden}"


def test_the_standalone_property_is_not_offered_to_the_model(connected_registry):
    schema = describe(connected_registry, "get_property_availability")

    assert (
        "south-boston-seaside-residence"
        not in schema["properties"]["property_slug"]["enum"]
    )


def test_guest_count_is_bounded_in_the_schema(connected_registry):
    guest = describe(connected_registry, "get_property_quote")["properties"][
        "guest_count"
    ]

    assert guest["type"] == "integer"
    assert guest["minimum"] == MIN_GUESTS
    assert guest["maximum"] == MAX_GUESTS


def test_no_free_text_argument_reaches_the_provider(connected_registry):
    """Dates are the only strings without an enum, and they are parsed."""
    for name in LODGIFY_TOOL_NAMES:
        properties = describe(connected_registry, name)["properties"]

        for field, definition in properties.items():
            if definition.get("type") != "string":
                continue

            assert "enum" in definition or field in {
                "start",
                "end",
                "arrival",
                "departure",
            }, f"{name}.{field} is unconstrained free text"


def test_definitions_hide_risk_from_the_model(connected_registry):
    for definition in connected_registry.definitions():
        assert "risk" not in definition.to_dict()


# -- agent integration -----------------------------------------------------


def test_the_agent_can_execute_a_lodgify_tool(agent_factory, connected_registry):
    model = ScriptedModelProvider(
        [
            tool_response("list_properties", {}, usage=fake_usage()),
            final_response("You have 8 properties.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, tool_registry=connected_registry).run(
        "List my properties.",
    )

    assert result["status"] == "COMPLETED"
    assert result["trace"][0]["tool"] == "list_properties"
    assert len(result["trace"][0]["result"]) == 8


def test_raw_provider_payload_never_reaches_the_trace(
    agent_factory, connected_registry
):
    model = ScriptedModelProvider(
        [
            tool_response(
                "get_property_availability",
                {"property_slug": SLUG, "start": "2026-09-12", "end": "2026-09-16"},
                usage=fake_usage(),
            ),
            final_response("Those dates look open.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, tool_registry=connected_registry).run(
        "Is it available?",
    )

    rendered = json.dumps(result["trace"])

    for leak in ("bookings", "22578370", "A Real Guest", "channel_calendars", "Airbnb"):
        assert leak not in rendered


def test_raw_provider_payload_never_reaches_the_audit_log(
    agent_factory, connected_registry, seeded_database
):
    from app.audit_store import AuditStore

    model = ScriptedModelProvider(
        [
            tool_response(
                "get_property_availability",
                {"property_slug": SLUG, "start": "2026-09-12", "end": "2026-09-16"},
                usage=fake_usage(),
            ),
            final_response("Those dates look open.", usage=fake_usage()),
        ]
    )

    agent_factory(model, tool_registry=connected_registry).run("Is it available?")

    rendered = json.dumps(AuditStore(database=seeded_database).list_events())

    for leak in ("bookings", "22578370", "A Real Guest", "channel_calendars", FAKE_KEY):
        assert leak not in rendered


def test_the_api_key_never_reaches_audit_or_trace(
    agent_factory, connected_registry, seeded_database
):
    from app.audit_store import AuditStore

    model = ScriptedModelProvider(
        [
            tool_response("list_properties", {}, usage=fake_usage()),
            final_response("Listed.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, tool_registry=connected_registry).run("List.")

    assert FAKE_KEY not in json.dumps(result)
    assert FAKE_KEY not in json.dumps(
        AuditStore(database=seeded_database).list_events()
    )


def test_a_provider_failure_lets_the_model_answer_rather_than_ending_the_run(
    agent_factory, migration_store
):
    """Fail-closed is a result, not a crash: the run still completes."""
    registry = build_tool_registry(
        migration_store=migration_store,
        lodgify=lodgify_tools(payload={"message": "boom"}, status=500),
    )

    model = ScriptedModelProvider(
        [
            tool_response(
                "get_property_availability",
                {"property_slug": SLUG, "start": "2026-09-12", "end": "2026-09-16"},
                usage=fake_usage(),
            ),
            final_response("I could not confirm availability.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, tool_registry=registry).run("Is it available?")

    assert result["status"] == "COMPLETED"

    tool_result = result["trace"][0]["result"]

    assert tool_result["ok"] is False
    assert tool_result["status"] == "unknown"


def test_a_bad_argument_is_recoverable_and_the_model_corrects(
    agent_factory, connected_registry
):
    model = ScriptedModelProvider(
        [
            tool_response(
                "get_property_availability",
                {
                    "property_slug": "south-boston-seaside-residence",
                    "start": "2026-09-12",
                    "end": "2026-09-16",
                },
                usage=fake_usage(),
            ),
            tool_response(
                "get_property_availability",
                {"property_slug": SLUG, "start": "2026-09-12", "end": "2026-09-16"},
                usage=fake_usage(),
            ),
            final_response("Checked the right property.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, tool_registry=connected_registry).run("Check it.")

    assert result["status"] == "COMPLETED"
    # Only the successful call is in the trace; the rejection is in the audit.
    assert len(result["trace"]) == 1


# -- observability ---------------------------------------------------------


def test_lodgify_executions_use_the_existing_tool_observability(
    agent_factory, connected_registry, seeded_database
):
    model = ScriptedModelProvider(
        [
            tool_response("list_properties", {}, usage=fake_usage()),
            final_response("Listed.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, tool_registry=connected_registry).run("List.")

    metrics = RunMetricsService(database=seeded_database).build(result["run_id"])

    assert metrics["tool_calls"] == 1
    assert metrics["tool_failures"] == 0
    assert metrics["tools"][0]["tool_name"] == "list_properties"
    assert metrics["tools"][0]["duration_ms"] is not None
    assert metrics["tools"][0]["status"] == "COMPLETED"


def test_observability_records_no_provider_payload(
    agent_factory, connected_registry, seeded_database
):
    model = ScriptedModelProvider(
        [
            tool_response(
                "get_property_availability",
                {"property_slug": SLUG, "start": "2026-09-12", "end": "2026-09-16"},
                usage=fake_usage(),
            ),
            final_response("Done.", usage=fake_usage()),
        ]
    )

    result = agent_factory(model, tool_registry=connected_registry).run("Check.")

    metrics = RunMetricsService(database=seeded_database).build(result["run_id"])

    rendered = json.dumps(metrics)

    for leak in ("bookings", "22578370", "A Real Guest", FAKE_KEY):
        assert leak not in rendered
