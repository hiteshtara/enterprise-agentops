"""The governed send: pinning, validation, exactly-once, and verification.

Every outcome that matters here is a safety property, not a feature. The suite
is written so that a change which makes `send_guest_reply` retry, rewrite
approved text, expose a provider identifier, or report an ambiguous send as a
success will fail loudly.

No test reaches Lodgify. Every POST goes to an httpx.MockTransport.
"""

import json

import httpx
import pytest

from app.connectors.lodgify.inbox import (
    MAX_MESSAGE_LENGTH,
    MAX_SUBJECT_LENGTH,
    validate_message,
    validate_subject,
)
from app.connectors.lodgify.messaging_client import (
    OWNER_MESSAGE_TYPE,
    SEND_CONTENT_TYPE,
    SEND_NOTIFICATION,
)
from app.connectors.lodgify.messaging_models import SendStatus
from app.connectors.lodgify.messaging_tools import SEND_REPLY_SCHEMA
from app.connectors.lodgify.refs import conversation_ref_for
from app.tool_registry import ToolRisk
from app.tool_setup import build_tool_registry
from tests.lodgify_fakes import (
    FAKE_KEY,
    THREAD_A,
    FakeLodgify,
    booking,
    message,
    thread,
)

REF = conversation_ref_for(1001)

SUBJECT = "Thank you"

BODY = "Thank you for your question. I'll check and get back to you shortly."

EXISTING = message(
    "m-guest-1",
    "Renter",
    "Is there parking?",
    "2026-09-01T10:00:00",
    message_status=None,
    route=None,
)


def sent_row(identifier="m-sent-1", created="2026-09-02T12:00:00", **kwargs):
    """A row shaped like the one the live send produced: route null, Delivered."""
    return message(
        identifier,
        "Owner",
        BODY,
        created,
        subject=SUBJECT,
        message_status=kwargs.pop("message_status", "Delivered"),
        route=kwargs.pop("route", None),
    )


def send_fake(after_messages, before_messages=None, **kwargs):
    """A fake whose thread read differs before and after the POST."""
    before = before_messages if before_messages is not None else [EXISTING]

    return FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        thread_sequence={
            THREAD_A: [
                thread(THREAD_A, before),
                thread(THREAD_A, after_messages),
            ]
        },
        threads={THREAD_A: thread(THREAD_A, before)},
        **kwargs,
    )


# -- 15/16. governance and schema -----------------------------------------


def test_send_tool_is_dangerous(migration_store):
    fake = send_fake([EXISTING, sent_row()])

    registry = build_tool_registry(
        migration_store=migration_store,
        lodgify_messaging=fake.tools(),
    )

    assert registry.get("send_guest_reply").risk is ToolRisk.DANGEROUS
    assert registry.get("list_recent_guest_conversations").risk is ToolRisk.READ
    assert registry.get("get_guest_conversation").risk is ToolRisk.READ


def test_send_schema_exposes_exactly_three_fields():
    assert set(SEND_REPLY_SCHEMA["properties"]) == {
        "conversation_ref",
        "subject",
        "message",
    }

    assert SEND_REPLY_SCHEMA["additionalProperties"] is False


def declared_property_names(schema) -> set[str]:
    """Every argument name the schema declares, at any nesting depth.

    Checked structurally rather than by searching the serialised text, because
    `type` is also one of JSON Schema's own keywords.
    """
    names: set[str] = set()

    if isinstance(schema, dict):
        properties = schema.get("properties")

        if isinstance(properties, dict):
            names |= set(properties)

        for value in schema.values():
            names |= declared_property_names(value)

    elif isinstance(schema, list):
        for item in schema:
            names |= declared_property_names(item)

    return names


@pytest.mark.parametrize(
    "forbidden",
    [
        "type",
        "send_notification",
        "booking_id",
        "thread_uid",
        "route",
        "message_status",
    ],
)
def test_send_schema_hides_every_provider_controlled_field(forbidden):
    assert forbidden not in declared_property_names(SEND_REPLY_SCHEMA)


def test_send_cannot_execute_without_approval(migration_store):
    from app.tool_registry import ApprovalRequired

    fake = send_fake([EXISTING, sent_row()])

    registry = build_tool_registry(
        migration_store=migration_store,
        lodgify_messaging=fake.tools(),
    )

    with pytest.raises(ApprovalRequired):
        registry.execute(
            "send_guest_reply",
            {"conversation_ref": REF, "subject": SUBJECT, "message": BODY},
        )

    # Nothing left the process.
    assert fake.posts == []
    assert fake.requests == []


def test_model_cannot_supply_type_or_send_notification(migration_store):
    fake = send_fake([EXISTING, sent_row()])

    registry = build_tool_registry(
        migration_store=migration_store,
        lodgify_messaging=fake.tools(),
    )

    with pytest.raises(TypeError):
        registry.execute(
            "send_guest_reply",
            {
                "conversation_ref": REF,
                "subject": SUBJECT,
                "message": BODY,
                "type": "Renter",
                "send_notification": False,
            },
            approved=True,
        )


# -- 17/18/19/20. the request that actually goes out ----------------------


def test_post_pins_owner_type_and_notification_and_sends_an_array():
    fake = send_fake([EXISTING, sent_row()])

    fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert len(fake.posts) == 1

    post = fake.posts[0]
    body = json.loads(post.content)

    assert isinstance(body, list)
    assert len(body) == 1

    assert body[0] == {
        "subject": SUBJECT,
        "message": BODY,
        "type": OWNER_MESSAGE_TYPE,
        "send_notification": SEND_NOTIFICATION,
    }

    assert OWNER_MESSAGE_TYPE == "Owner"
    assert SEND_NOTIFICATION is True

    assert post.headers["content-type"] == SEND_CONTENT_TYPE
    assert post.headers["X-ApiKey"] == FAKE_KEY
    assert post.url.path == "/v1/reservation/booking/1001/messages"


def test_approved_text_is_transmitted_byte_for_byte():
    awkward = "Hi there,\n\n  Parking is shared — no charge.\n\nSee you soon!"

    after = message(
        "m-sent-1",
        "Owner",
        awkward,
        "2026-09-02T12:00:00",
        subject=SUBJECT,
        message_status="Delivered",
        route=None,
    )

    fake = send_fake([EXISTING, after])

    fake.inbox().send_reply(REF, SUBJECT, awkward)

    assert json.loads(fake.posts[0].content)[0]["message"] == awkward


# -- 21/22/23/24/25. exactly once, verified -------------------------------


def test_confirmed_sent_for_one_new_matching_row():
    fake = send_fake([EXISTING, sent_row()])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.CONFIRMED_SENT.value
    assert len(result["messages"]) == 1
    assert result["messages"][0]["message_status"] == "Delivered"
    assert result["message"] == "Lodgify reports the message as Delivered."


def test_thread_is_snapshotted_before_and_reread_after():
    fake = send_fake([EXISTING, sent_row()])

    fake.inbox().send_reply(REF, SUBJECT, BODY)

    methods = [request.method for request in fake.requests]

    # bookings GET, snapshot GET, the POST, then the verification GET.
    assert methods.count("POST") == 1
    assert methods.index("POST") < len(methods) - 1
    assert len(fake.thread_reads) == 2


def test_route_null_with_delivered_is_accepted():
    row = sent_row(message_status="Delivered")

    assert row["route"] is None

    fake = send_fake([EXISTING, row])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    # route is never a delivery predicate, and never surfaces at all.
    assert result["status"] == SendStatus.CONFIRMED_SENT.value
    assert "route" not in json.dumps(result)


def test_fan_out_reports_every_created_row():
    first = sent_row("m-sent-1", "2026-09-02T12:00:00", message_status="Sent")
    second = sent_row("m-sent-2", "2026-09-02T12:00:00", message_status="Delivered")

    fake = send_fake([EXISTING, first, second])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.CONFIRMED_SENT.value
    assert len(result["messages"]) == 2

    # The weakest true claim wins: one row only reached "Sent".
    assert result["message"] == "Lodgify reports the message as Sent."


def test_failed_status_is_reported_as_failed():
    fake = send_fake([EXISTING, sent_row(message_status="Failed")])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["message"] == "Lodgify reports the message as Failed."


def test_missing_delivery_status_is_not_claimed_as_delivered():
    fake = send_fake([EXISTING, sent_row(message_status=None)])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["messages"][0]["message_status"] is None
    assert "Delivered" not in result["message"]


def test_result_never_names_an_ota_channel():
    fake = send_fake([EXISTING, sent_row()])

    body = json.dumps(fake.inbox().send_reply(REF, SUBJECT, BODY))

    for channel in ("Airbnb", "Vrbo", "Booking.com", "BookingCom"):
        assert channel not in body


# -- 26/29/30/31. the ambiguous paths -------------------------------------


def test_timeout_after_post_is_unknown_and_is_not_retried():
    fake = send_fake(
        [EXISTING],
        post_raises=httpx.ReadTimeout("timed out"),
    )

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value
    assert "Do not resend automatically" in result["message"]

    # Exactly one attempt. A retry here could deliver a second real message.
    assert len(fake.posts) == 1


def test_server_error_is_unknown_not_failed():
    fake = send_fake([EXISTING], post_status=500)

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value
    assert len(fake.posts) == 1


def test_connection_error_is_a_clean_failure():
    fake = send_fake(
        [EXISTING],
        post_raises=httpx.ConnectError("refused"),
    )

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    # The connection was never established, so nothing can have been sent.
    assert result["status"] == SendStatus.CONFIRMED_FAILED.value


def test_provider_rejection_is_a_clean_failure():
    fake = send_fake([EXISTING], post_status=400)

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.CONFIRMED_FAILED.value
    assert len(fake.posts) == 1


def test_verification_finding_nothing_is_unknown():
    # The POST succeeded but no matching row appeared.
    fake = send_fake([EXISTING])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value
    assert result["messages"] == []


def test_unreadable_thread_after_send_is_unknown():
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        thread_sequence={THREAD_A: [thread(THREAD_A, [EXISTING])]},
        threads={},
    )

    # The verification read returns an empty thread rather than the send.
    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value


def test_unreadable_thread_before_send_sends_nothing():
    fake = FakeLodgify(bookings=[booking(1001, THREAD_A)], thread_status=503)

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.CONFIRMED_FAILED.value
    assert fake.posts == []


def test_widely_spaced_matching_rows_are_ambiguous():
    # Two identical Owner rows minutes apart is not fan-out; someone else
    # probably sent one. Refuse to attribute rather than guess.
    first = sent_row("m-sent-1", "2026-09-02T12:00:00")
    late = sent_row("m-sent-2", "2026-09-02T12:30:00")

    fake = send_fake([EXISTING, first, late])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value


def test_implausibly_many_matching_rows_are_ambiguous():
    rows = [sent_row(f"m-sent-{n}", "2026-09-02T12:00:00") for n in range(6)]

    fake = send_fake([EXISTING, *rows])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value


def test_a_concurrent_unrelated_message_is_not_misattributed():
    ours = sent_row()

    someone_else = message(
        "m-other",
        "Owner",
        "Completely different message about the driveway.",
        "2026-09-02T12:00:01",
        subject="Driveway",
    )

    fake = send_fake([EXISTING, ours, someone_else])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.CONFIRMED_SENT.value
    assert len(result["messages"]) == 1


def test_a_pre_existing_identical_message_is_not_claimed_as_ours():
    old = sent_row("m-old", "2026-09-01T09:00:00")

    # The same text already existed and nothing new arrived.
    fake = send_fake([EXISTING, old], before_messages=[EXISTING, old])

    result = fake.inbox().send_reply(REF, SUBJECT, BODY)

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value


# -- validation ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\n\n"])
def test_empty_subject_and_message_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_subject(bad)

    with pytest.raises(ValueError):
        validate_message(bad)


def test_over_length_is_rejected():
    with pytest.raises(ValueError):
        validate_subject("x" * (MAX_SUBJECT_LENGTH + 1))

    with pytest.raises(ValueError):
        validate_message("x" * (MAX_MESSAGE_LENGTH + 1))


@pytest.mark.parametrize("bad", ["null\x00byte", "bell\x07", "carriage\r\nreturn"])
def test_control_characters_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_message(bad)


def test_html_is_rejected():
    with pytest.raises(ValueError):
        validate_message("<p>Hello</p>")

    with pytest.raises(ValueError):
        validate_subject("<b>Hi</b>")


def test_newlines_are_allowed_in_the_body_but_not_the_subject():
    assert validate_message("Line one.\n\nLine two.") == "Line one.\n\nLine two."

    with pytest.raises(ValueError):
        validate_subject("Two\nlines")


def test_validation_returns_the_value_unchanged():
    # Never rewrite: the approval card's text must be the transmitted text.
    text = "  Leading and trailing spaces are preserved.  "

    assert validate_message(text) == text


def test_non_string_arguments_raise_type_error():
    with pytest.raises(TypeError):
        validate_message(42)

    with pytest.raises(TypeError):
        validate_subject(None)


def test_invalid_arguments_send_nothing():
    fake = send_fake([EXISTING, sent_row()])

    with pytest.raises(ValueError):
        fake.inbox().send_reply(REF, "", BODY)

    assert fake.requests == []


def test_an_unknown_ref_sends_nothing():
    fake = send_fake([EXISTING, sent_row()])

    with pytest.raises(ValueError):
        fake.inbox().send_reply("PH-ZZZZZZZZ", SUBJECT, BODY)

    assert fake.posts == []


# -- 34/35. leak checks ----------------------------------------------------


def test_send_result_carries_no_provider_identifier_or_credential():
    fake = send_fake([EXISTING, sent_row()])

    body = json.dumps(fake.inbox().send_reply(REF, SUBJECT, BODY))

    assert "1001" not in body
    assert THREAD_A not in body
    assert FAKE_KEY not in body
    assert "m-sent-1" not in body
    assert "fixture.guest@example.invalid" not in body
