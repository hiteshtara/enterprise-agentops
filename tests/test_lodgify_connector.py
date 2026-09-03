"""Lodgify connector: auth, parameter spellings, sanitization, fail-closed.

Every test drives the client through an httpx MockTransport. No test opens a
socket, and no test uses a real credential.
"""

import json

import httpx
import pytest

from app.connectors.lodgify.client import LodgifyClient
from app.connectors.lodgify.config import (
    LODGIFY_PROPERTIES,
    LODGIFY_SLUGS,
    MAX_AVAILABILITY_DAYS,
    MAX_GUESTS,
    STANDALONE_PROPERTIES,
    find_lodgify_property,
    is_configured,
    resolve_api_key,
)
from app.connectors.lodgify.errors import (
    LodgifyConfigurationError,
    LodgifyRejected,
    LodgifyUnavailable,
)
from app.connectors.lodgify.tools import LodgifyTools

FAKE_KEY = "test-only-not-a-real-lodgify-key"

SLUG = "boston-condo-second-floor"

PROPERTY_ID = 681293

ROOM_TYPE_ID = 748340


class Recorder:
    """Captures the request the client actually sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def last(self) -> httpx.Request:
        return self.requests[-1]


def transport_returning(
    payload=None,
    status: int = 200,
    recorder: Recorder | None = None,
    raise_exc: Exception | None = None,
    text: str | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.requests.append(request)

        if raise_exc is not None:
            raise raise_exc

        if text is not None:
            return httpx.Response(status, text=text)

        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def build_tools(
    payload=None,
    status: int = 200,
    recorder: Recorder | None = None,
    raise_exc: Exception | None = None,
    text: str | None = None,
    key_provider=None,
) -> LodgifyTools:
    client = LodgifyClient(
        api_key_provider=key_provider or (lambda: FAKE_KEY),
        transport=transport_returning(payload, status, recorder, raise_exc, text),
    )

    return LodgifyTools(client)


# Shaped like the real upstream response, including fields that must be dropped.
AVAILABILITY_PAYLOAD = [
    {
        "property_id": PROPERTY_ID,
        "periods": [
            {
                "start": "2026-09-12",
                "end": "2026-09-14",
                "available": 1,
                # Everything below must never escape the connector.
                "bookings": [{"id": 22578370, "guest_name": "A Real Guest"}],
                "channel_calendars": [{"channel": "Airbnb", "source": "ical"}],
                "notes": "internal owner note",
            },
            {"start": "2026-09-15", "end": "2026-09-16", "available": 0},
        ],
        "user_id": 99,
    }
]

QUOTE_PAYLOAD = [
    {
        "currency_code": "USD",
        "total_excluding_vat": 777.08,
        "room_types": [
            {
                "price_types": [
                    {"type": 0, "description": "Room rate", "subtotal": 504.0},
                    {"type": 2, "description": "Cleaning fee", "subtotal": 200.0},
                    {"type": 4, "description": "Hotel tax", "subtotal": 73.08},
                ]
            }
        ],
        # Must never be surfaced.
        "cancellation_policy_text": "Full refund up to 30 days...",
        "scheduled_payments": [{"amount": 388.54, "due": "2026-09-01"}],
        "guest": {"name": "A Real Guest", "email": "guest@example.com"},
        "booking_id": 22578370,
    }
]


# -- authentication --------------------------------------------------------


def test_api_key_is_sent_as_the_x_apikey_header():
    recorder = Recorder()

    build_tools(AVAILABILITY_PAYLOAD, recorder=recorder).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert recorder.last().headers["X-ApiKey"] == FAKE_KEY


def test_a_useful_user_agent_is_sent():
    recorder = Recorder()

    build_tools(AVAILABILITY_PAYLOAD, recorder=recorder).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert "AgentGuard" in recorder.last().headers["User-Agent"]


def test_the_api_key_never_appears_in_a_result():
    result = build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert FAKE_KEY not in json.dumps(result)


def test_the_api_key_never_appears_in_a_quote_result():
    result = build_tools(QUOTE_PAYLOAD).get_property_quote(
        SLUG, "2026-09-12", "2026-09-14", 2
    )

    assert FAKE_KEY not in json.dumps(result)


def test_an_unset_key_is_a_configuration_error_not_a_fake_result(monkeypatch):
    monkeypatch.delenv("LODGIFY_API_KEY", raising=False)

    assert not is_configured()

    with pytest.raises(LodgifyConfigurationError, match="LODGIFY_API_KEY"):
        resolve_api_key()


def test_a_configured_key_is_resolved(monkeypatch):
    monkeypatch.setenv("LODGIFY_API_KEY", FAKE_KEY)

    assert is_configured()
    assert resolve_api_key() == FAKE_KEY


def test_an_unconfigured_connector_reports_unknown_rather_than_failing(monkeypatch):
    monkeypatch.delenv("LODGIFY_API_KEY", raising=False)

    tools = build_tools(AVAILABILITY_PAYLOAD, key_provider=resolve_api_key)

    result = tools.get_property_availability(SLUG, "2026-09-12", "2026-09-16")

    assert result["ok"] is False
    assert result["reason"] == "not_configured"
    assert "available" not in result


# -- availability parameters -----------------------------------------------


def test_availability_uses_start_and_end_not_from_and_to():
    recorder = Recorder()

    build_tools(AVAILABILITY_PAYLOAD, recorder=recorder).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    params = recorder.last().url.params

    assert params["start"] == "2026-09-12"
    assert params["end"] == "2026-09-16"
    # from/to are silently ignored upstream and return placeholder data.
    assert "from" not in params
    assert "to" not in params


def test_availability_sends_include_details():
    recorder = Recorder()

    build_tools(AVAILABILITY_PAYLOAD, recorder=recorder).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert recorder.last().url.params["includeDetails"] == "true"


def test_availability_addresses_the_resolved_property_id():
    recorder = Recorder()

    build_tools(AVAILABILITY_PAYLOAD, recorder=recorder).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert recorder.last().url.path == f"/v2/availability/{PROPERTY_ID}"


# -- availability sanitization ---------------------------------------------


def test_availability_returns_only_start_end_available():
    result = build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert result["ok"] is True
    assert len(result["periods"]) == 2

    for period in result["periods"]:
        assert set(period) == {"start", "end", "available"}

    assert result["periods"][0]["available"] is True
    assert result["periods"][1]["available"] is False


def test_upstream_booking_and_guest_fields_are_discarded():
    result = build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    rendered = json.dumps(result)

    for leak in (
        "bookings",
        "22578370",
        "A Real Guest",
        "channel_calendars",
        "Airbnb",
        "internal owner note",
        "user_id",
        "property_id",
    ):
        assert leak not in rendered, f"{leak} leaked into the availability result"


def test_an_unexpected_upstream_field_cannot_survive():
    payload = [
        {
            "periods": [
                {
                    "start": "2026-09-12",
                    "end": "2026-09-13",
                    "available": 1,
                    "some_future_field": "should not appear",
                }
            ]
        }
    ]

    result = build_tools(payload).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert "some_future_field" not in json.dumps(result)


# -- availability fail-closed ----------------------------------------------


def failure_result(**kwargs) -> dict:
    return build_tools(**kwargs).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )


def has_key(value, name: str) -> bool:
    """Whether a key appears anywhere in a nested structure."""
    if isinstance(value, dict):
        return name in value or any(has_key(v, name) for v in value.values())

    if isinstance(value, list):
        return any(has_key(item, name) for item in value)

    return False


def assert_fails_closed(result: dict) -> None:
    assert result["ok"] is False
    assert result["status"] == "unknown"
    # The critical property: a failure carries no availability claim at all.
    # Checked structurally -- a substring test would match "unavailable".
    assert not has_key(result, "available")
    assert not has_key(result, "periods")


def test_a_timeout_fails_closed():
    assert_fails_closed(failure_result(raise_exc=httpx.TimeoutException("timed out")))


def test_a_transport_error_fails_closed():
    assert_fails_closed(failure_result(raise_exc=httpx.ConnectError("refused")))


def test_a_500_fails_closed():
    assert_fails_closed(failure_result(payload={"message": "boom"}, status=500))


def test_a_401_fails_closed():
    assert_fails_closed(failure_result(payload={"message": "nope"}, status=401))


def test_malformed_json_fails_closed():
    assert_fails_closed(failure_result(text="<html>not json</html>"))


def test_an_unexpected_shape_fails_closed():
    assert_fails_closed(failure_result(payload={"unexpected": "object"}))


def test_a_non_numeric_available_flag_fails_closed():
    payload = [{"periods": [{"start": "a", "end": "b", "available": "yes"}]}]

    assert_fails_closed(failure_result(payload=payload))


def test_a_provider_failure_never_reports_available_true():
    for exc in (httpx.TimeoutException("t"), httpx.ConnectError("c")):
        result = failure_result(raise_exc=exc)

        assert result.get("periods") is None
        assert not has_key(result, "available")
        assert True not in result.values()


def test_an_empty_period_list_is_a_real_answer_not_a_failure():
    result = build_tools([{"periods": []}]).get_property_availability(
        SLUG, "2026-09-12", "2026-09-16"
    )

    assert result["ok"] is True
    assert result["periods"] == []


# -- quote parameters ------------------------------------------------------


def test_quote_uses_arrival_and_departure():
    recorder = Recorder()

    build_tools(QUOTE_PAYLOAD, recorder=recorder).get_property_quote(
        SLUG, "2026-09-12", "2026-09-14", 2
    )

    params = recorder.last().url.params

    assert params["arrival"] == "2026-09-12"
    assert params["departure"] == "2026-09-14"
    assert "from" not in params
    assert "to" not in params


def test_quote_uses_the_exact_guestbreakdown_spelling():
    recorder = Recorder()

    build_tools(QUOTE_PAYLOAD, recorder=recorder).get_property_quote(
        SLUG, "2026-09-12", "2026-09-14", 4
    )

    query = str(recorder.last().url)

    # The underscored form returns a 500 upstream, not a clean 400.
    assert "roomTypes%5B0%5D.guestbreakdown.adults=4" in query or (
        "roomTypes[0].guestbreakdown.adults=4" in query
    )
    assert "guest_breakdown" not in query


def test_quote_sends_the_resolved_room_type_id():
    recorder = Recorder()

    build_tools(QUOTE_PAYLOAD, recorder=recorder).get_property_quote(
        SLUG, "2026-09-12", "2026-09-14", 2
    )

    assert recorder.last().url.params["roomTypes[0].Id"] == str(ROOM_TYPE_ID)
    assert recorder.last().url.path == f"/v2/quote/{PROPERTY_ID}"


# -- quote sanitization ----------------------------------------------------


def test_quote_returns_only_business_safe_pricing_fields():
    result = build_tools(QUOTE_PAYLOAD).get_property_quote(
        SLUG, "2026-09-12", "2026-09-14", 2
    )

    assert set(result) == {
        "ok",
        "property_slug",
        "arrival",
        "departure",
        "guest_count",
        "currency",
        "accommodation_amount",
        "cleaning_fee",
        "taxes",
        "total",
    }
    assert result["currency"] == "USD"
    assert result["accommodation_amount"] == 504.0
    assert result["cleaning_fee"] == 200.0
    assert result["taxes"] == 73.08
    assert result["total"] == 777.08


def test_quote_discards_policy_payment_and_guest_data():
    rendered = json.dumps(
        build_tools(QUOTE_PAYLOAD).get_property_quote(
            SLUG, "2026-09-12", "2026-09-14", 2
        )
    )

    for leak in (
        "cancellation_policy_text",
        "scheduled_payments",
        "A Real Guest",
        "guest@example.com",
        "booking_id",
        "22578370",
    ):
        assert leak not in rendered, f"{leak} leaked into the quote result"


def test_a_business_rule_rejection_is_declined_not_unknown():
    result = build_tools(
        {"message": "The minimum stay for this rental is 2 days"}, status=400
    ).get_property_quote(SLUG, "2026-09-12", "2026-09-13", 2)

    assert result["ok"] is False
    assert result["status"] == "declined"
    assert result["reason"] == "min_stay"
    # The provider's own wording is translated, not forwarded.
    assert "rental is 2 days" not in result["message"]


def test_quote_provider_failure_fails_closed():
    result = build_tools(raise_exc=httpx.TimeoutException("t")).get_property_quote(
        SLUG, "2026-09-12", "2026-09-14", 2
    )

    assert result["ok"] is False
    assert result["status"] == "unknown"
    assert "total" not in result


def test_a_malformed_quote_shape_fails_closed():
    result = build_tools({"currency_code": "USD"}).get_property_quote(
        SLUG, "2026-09-12", "2026-09-14", 2
    )

    assert result["ok"] is False
    assert result["status"] == "unknown"


# -- property boundary -----------------------------------------------------


def test_an_unknown_slug_is_rejected():
    with pytest.raises(ValueError, match="Unknown property"):
        build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
            "not-a-real-property", "2026-09-12", "2026-09-16"
        )


def test_the_standalone_property_is_rejected():
    """South Boston has no Lodgify id and must never be queried through it."""
    standalone = STANDALONE_PROPERTIES[0].slug

    with pytest.raises(ValueError, match="not booked through Lodgify"):
        build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
            standalone, "2026-09-12", "2026-09-16"
        )

    with pytest.raises(ValueError, match="not booked through Lodgify"):
        build_tools(QUOTE_PAYLOAD).get_property_quote(
            standalone, "2026-09-12", "2026-09-14", 2
        )


def test_a_numeric_property_id_is_not_a_valid_slug():
    """The model cannot address the provider directly."""
    for candidate in (str(PROPERTY_ID), PROPERTY_ID):
        with pytest.raises((ValueError, TypeError)):
            build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
                candidate, "2026-09-12", "2026-09-16"
            )


def test_the_standalone_property_has_no_provider_identifiers():
    for standalone in STANDALONE_PROPERTIES:
        assert not hasattr(standalone, "lodgify_property_id")
        assert not hasattr(standalone, "room_type_id")


def test_every_configured_property_resolves():
    for prop in LODGIFY_PROPERTIES:
        assert find_lodgify_property(prop.slug) is prop


def test_the_configuration_matches_the_audited_source():
    """Guards the ids copied from the Priyanka Homes repository."""
    expected = {
        "renovated-3rd-floor-retreat-3-beds-roslindale-village": (680420, 747399),
        "renovated-2nd-floor-home": (680434, 747413),
        "budget-friendly-basement-2br-retreat": (680444, 747423),
        "modern-condo-walk-out-basement-near-train": (680447, 747426),
        "boston-hospitality-homes-harvard": (681286, 748333),
        "boston-condo-second-floor": (681293, 748340),
        "arboretum-retreat-city-of-boston": (681301, 748348),
    }

    actual = {
        p.slug: (p.lodgify_property_id, p.room_type_id) for p in LODGIFY_PROPERTIES
    }

    assert actual == expected
    assert len(LODGIFY_PROPERTIES) == 7
    assert len(STANDALONE_PROPERTIES) == 1


# -- argument validation ---------------------------------------------------


def test_a_malformed_date_is_rejected():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
            SLUG, "12/09/2026", "2026-09-16"
        )


def test_an_inverted_range_is_rejected():
    with pytest.raises(ValueError, match="end must be after start"):
        build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
            SLUG, "2026-09-16", "2026-09-12"
        )

    with pytest.raises(ValueError, match="departure must be after arrival"):
        build_tools(QUOTE_PAYLOAD).get_property_quote(
            SLUG, "2026-09-14", "2026-09-12", 2
        )


def test_the_availability_window_is_bounded():
    with pytest.raises(ValueError, match=f"{MAX_AVAILABILITY_DAYS} days or fewer"):
        build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
            SLUG, "2026-09-01", "2026-11-01"
        )


def test_the_maximum_window_itself_is_accepted():
    result = build_tools(AVAILABILITY_PAYLOAD).get_property_availability(
        SLUG, "2026-09-01", "2026-10-02"
    )

    assert result["ok"] is True


def test_guest_count_bounds_are_enforced():
    tools = build_tools(QUOTE_PAYLOAD)

    for bad in (0, -1, MAX_GUESTS + 1):
        with pytest.raises(ValueError, match="guest_count must be between"):
            tools.get_property_quote(SLUG, "2026-09-12", "2026-09-14", bad)


def test_a_non_integer_guest_count_is_rejected():
    tools = build_tools(QUOTE_PAYLOAD)

    for bad in ("four", 2.5, True):
        with pytest.raises(TypeError, match="guest_count must be an integer"):
            tools.get_property_quote(SLUG, "2026-09-12", "2026-09-14", bad)


def test_no_provider_call_is_made_when_validation_fails():
    recorder = Recorder()

    with pytest.raises(ValueError):
        build_tools(AVAILABILITY_PAYLOAD, recorder=recorder).get_property_availability(
            "not-a-real-property", "2026-09-12", "2026-09-16"
        )

    assert recorder.requests == []


# -- list_properties -------------------------------------------------------


def test_list_properties_makes_no_provider_call():
    recorder = Recorder()

    build_tools(AVAILABILITY_PAYLOAD, recorder=recorder).list_properties()

    assert recorder.requests == []


def test_list_properties_reports_every_property_and_its_connection():
    listed = build_tools().list_properties()

    assert len(listed) == 8

    connected = [p for p in listed if p["lodgify_connected"]]
    standalone = [p for p in listed if not p["lodgify_connected"]]

    assert len(connected) == 7
    assert len(standalone) == 1
    assert standalone[0]["slug"] == "south-boston-seaside-residence"


def test_list_properties_exposes_no_provider_identifiers():
    rendered = json.dumps(build_tools().list_properties())

    for prop in LODGIFY_PROPERTIES:
        assert str(prop.lodgify_property_id) not in rendered
        assert str(prop.room_type_id) not in rendered

    for entry in build_tools().list_properties():
        assert set(entry) == {"slug", "name", "lodgify_connected"}


def test_the_slug_enum_matches_the_configured_properties():
    assert set(LODGIFY_SLUGS) == {p.slug for p in LODGIFY_PROPERTIES}


# -- error hygiene ---------------------------------------------------------


def test_provider_errors_carry_no_stack_trace_or_url():
    result = build_tools(
        raise_exc=httpx.ConnectError("connection to api.lodgify.com:443 refused")
    ).get_property_availability(SLUG, "2026-09-12", "2026-09-16")

    rendered = json.dumps(result)

    assert "Traceback" not in rendered
    assert "api.lodgify.com" not in rendered
    assert "refused" not in rendered


def test_rejection_carries_a_reason_not_the_providers_text():
    exc = None

    try:
        LodgifyClient(
            api_key_provider=lambda: FAKE_KEY,
            transport=transport_returning(
                {"message": "The number of people is too high", "code": 666}, 400
            ),
        ).get_quote(PROPERTY_ID, ROOM_TYPE_ID, "2026-09-12", "2026-09-14", 99)
    except LodgifyRejected as caught:
        exc = caught

    assert exc is not None
    assert exc.reason == "guest_limit"
    assert "666" not in str(exc)


def test_unavailable_is_raised_for_a_bad_status():
    with pytest.raises(LodgifyUnavailable):
        LodgifyClient(
            api_key_provider=lambda: FAKE_KEY,
            transport=transport_returning({"message": "server error"}, 503),
        ).get_availability(PROPERTY_ID, "2026-09-12", "2026-09-16")
