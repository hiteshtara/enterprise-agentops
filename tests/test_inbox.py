"""Inbox reads: discovery, sanitization, ordering and the needs-attention rule.

No test reaches Lodgify. Everything runs through httpx.MockTransport.
"""

import json

import pytest

from app.connectors.lodgify.inbox import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    LodgifyInbox,
    classify_conversation,
    plain_text,
    read_messages,
)
from app.connectors.lodgify.messaging_client import LodgifyMessagingClient
from app.connectors.lodgify.messaging_models import ConversationStatus
from app.connectors.lodgify.refs import conversation_ref_for, is_well_formed
from tests.lodgify_fakes import (
    BOSTON_CONDO_ID,
    FAKE_KEY,
    ROSLINDALE_ID,
    THREAD_A,
    THREAD_B,
    FakeLodgify,
    booking,
    message,
    thread,
)

GUEST_QUESTION = message(
    "m-guest-1",
    "Renter",
    "Is there parking at the house?",
    "2026-09-01T10:00:00",
    message_status=None,
    route=None,
)

OWNER_ANSWER = message(
    "m-owner-1",
    "Owner",
    "Parking is shared and there is no extra charge.",
    "2026-09-01T11:00:00",
)


def one_thread_fake(messages, thread_uid=THREAD_A, **booking_kwargs):
    return FakeLodgify(
        bookings=[booking(1001, thread_uid, **booking_kwargs)],
        threads={thread_uid: thread(thread_uid, messages)},
    )


# -- 1. thread endpoint shape ---------------------------------------------


def test_reads_use_the_documented_supported_endpoints():
    fake = one_thread_fake([GUEST_QUESTION])

    fake.inbox().list_conversations()

    paths = [request.url.path for request in fake.requests]

    assert "/v2/reservations/bookings" in paths
    assert f"/v2/messaging/{THREAD_A}" in paths

    # Supported api.lodgify.com only -- never a private dashboard endpoint.
    for request in fake.requests:
        assert request.url.host == "api.lodgify.com"
        assert "app.lodgify.com" not in str(request.url)


def test_reads_authenticate_with_the_api_key_header():
    fake = one_thread_fake([GUEST_QUESTION])

    fake.inbox().list_conversations()

    assert fake.requests[0].headers["X-ApiKey"] == FAKE_KEY
    assert "Authorization" not in fake.requests[0].headers
    assert "Cookie" not in fake.requests[0].headers


# -- 2/3/4. booking sanitization ------------------------------------------


def test_booking_sanitization_drops_guest_contact_and_source_text():
    fake = one_thread_fake([GUEST_QUESTION])

    rows = fake.inbox().list_conversations()
    body = json.dumps(rows)

    assert "fixture.guest@example.invalid" not in body
    assert "+15550000000" not in body
    assert "Fixture Guest" not in body
    assert "203.0.113.10" not in body
    assert "internal note that must not travel" not in body

    # source_text is untrusted free text and is never read or forwarded.
    assert "HMFAKE0000" not in body
    assert "listingId" not in body
    assert "source_text" not in body

    # Financial fields are not part of a conversation.
    assert "1234.56" not in body


def test_summary_exposes_only_the_agreed_fields():
    fake = one_thread_fake([GUEST_QUESTION])

    row = fake.inbox().list_conversations()[0]

    assert set(row) == {
        "conversation_ref",
        "property_slug",
        "property_name",
        "source",
        "booking_status",
        "status",
        "last_message_at",
        "last_message_sender",
        "last_message_excerpt",
        "message_count",
    }


# -- 4/5/6/7. provider identifiers stay internal --------------------------


def test_provider_identifiers_never_appear_in_results():
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        threads={THREAD_A: thread(THREAD_A, [GUEST_QUESTION])},
    )

    inbox = fake.inbox()

    listed = json.dumps(inbox.list_conversations())
    detail = json.dumps(inbox.get_conversation(conversation_ref_for(1001)))

    for body in (listed, detail):
        assert "1001" not in body
        assert THREAD_A not in body
        assert "thread_uid" not in body
        assert "booking_id" not in body


def test_conversation_ref_is_stable_and_well_formed():
    first = conversation_ref_for(1001)
    second = conversation_ref_for(1001)

    assert first == second
    assert is_well_formed(first)
    assert first != conversation_ref_for(1002)


def test_booking_to_thread_resolution_uses_the_ref():
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A), booking(1002, THREAD_B)],
        threads={
            THREAD_A: thread(THREAD_A, [GUEST_QUESTION]),
            THREAD_B: thread(THREAD_B, [OWNER_ANSWER]),
        },
    )

    resolved = fake.inbox().resolve(conversation_ref_for(1002))

    assert resolved.booking_id == 1002
    assert resolved.thread_uid == THREAD_B


def test_a_fabricated_ref_is_recoverable_and_reaches_no_thread():
    fake = one_thread_fake([GUEST_QUESTION])

    inbox = fake.inbox()

    with pytest.raises(ValueError):
        inbox.get_conversation("PH-AAAAAAAA")

    # It resolved against real bookings and stopped. No thread was fetched.
    assert fake.thread_reads == []


def test_a_malformed_ref_is_rejected_before_any_provider_call():
    fake = one_thread_fake([GUEST_QUESTION])

    inbox = fake.inbox()

    with pytest.raises(ValueError):
        inbox.get_conversation("../../etc/passwd")

    assert fake.requests == []


# -- 7. property filtering + bounded limit --------------------------------


def test_property_filter_selects_only_that_property():
    fake = FakeLodgify(
        bookings=[
            booking(1001, THREAD_A, property_id=ROSLINDALE_ID),
            booking(1002, THREAD_B, property_id=BOSTON_CONDO_ID),
        ],
        threads={
            THREAD_A: thread(THREAD_A, [GUEST_QUESTION]),
            THREAD_B: thread(THREAD_B, [OWNER_ANSWER]),
        },
    )

    rows = fake.inbox().list_conversations(
        property_slug="boston-condo-second-floor",
    )

    assert [row["property_slug"] for row in rows] == ["boston-condo-second-floor"]


def test_unknown_property_slug_is_recoverable():
    fake = one_thread_fake([GUEST_QUESTION])

    with pytest.raises(ValueError):
        fake.inbox().list_conversations(property_slug="not-a-property")


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
def test_limit_is_bounded(limit):
    fake = one_thread_fake([GUEST_QUESTION])

    with pytest.raises(ValueError):
        fake.inbox().list_conversations(limit=limit)


def test_limit_rejects_non_integers():
    fake = one_thread_fake([GUEST_QUESTION])

    with pytest.raises(TypeError):
        fake.inbox().list_conversations(limit="20")


def test_limit_bounds_how_many_threads_are_read():
    fake = FakeLodgify(
        bookings=[booking(1000 + n, f"thread-{n}") for n in range(10)],
        threads={
            f"thread-{n}": thread(f"thread-{n}", [GUEST_QUESTION]) for n in range(10)
        },
    )

    rows = fake.inbox().list_conversations(limit=3)

    assert len(rows) == 3
    # One thread read per returned row, and no more.
    assert len(fake.thread_reads) == 3


def test_default_limit_is_applied():
    fake = FakeLodgify(
        bookings=[booking(1000 + n, f"thread-{n}") for n in range(30)],
        threads={
            f"thread-{n}": thread(f"thread-{n}", [GUEST_QUESTION]) for n in range(30)
        },
    )

    assert len(fake.inbox().list_conversations()) == DEFAULT_LIMIT


# -- 9/10. needs-attention rule -------------------------------------------


def test_guest_last_means_needs_attention():
    follow_up = message(
        "m-guest-2",
        "Renter",
        "One more thing -- is the driveway shared?",
        "2026-09-01T12:00:00",
        message_status=None,
        route=None,
    )

    fake = one_thread_fake([GUEST_QUESTION, OWNER_ANSWER, follow_up])

    row = fake.inbox().list_conversations()[0]

    assert row["status"] == ConversationStatus.NEEDS_ATTENTION.value
    assert row["last_message_sender"] == "Renter"


def test_owner_last_means_responded():
    fake = one_thread_fake([GUEST_QUESTION, OWNER_ANSWER])

    assert (
        fake.inbox().list_conversations()[0]["status"]
        == ConversationStatus.RESPONDED.value
    )


def test_empty_thread_is_unknown_not_needs_attention():
    fake = one_thread_fake([])

    assert (
        fake.inbox().list_conversations()[0]["status"]
        == ConversationStatus.UNKNOWN.value
    )


def test_unrecognised_sender_is_unknown_not_needs_attention():
    odd = message("m-x", "SomethingNew", "hello", "2026-09-01T10:00:00")

    fake = one_thread_fake([odd])

    row = fake.inbox().list_conversations()[0]

    assert row["status"] == ConversationStatus.UNKNOWN.value
    assert row["last_message_sender"] is None


def test_unreadable_thread_is_unknown_not_needs_attention():
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        thread_status=500,
    )

    row = fake.inbox().list_conversations()[0]

    assert row["status"] == ConversationStatus.UNKNOWN.value
    assert row["last_message_excerpt"] is None
    assert row["message_count"] == 0


def test_classification_never_guesses_from_read_state():
    # is_read reflects whether a human opened the thread in Lodgify, not
    # whether the guest was answered, so it must not move the classification.
    read = classify_conversation(
        read_messages(thread(THREAD_A, [GUEST_QUESTION], is_read=True))
    )
    unread = classify_conversation(
        read_messages(thread(THREAD_A, [GUEST_QUESTION], is_read=False))
    )

    assert read == unread == ConversationStatus.NEEDS_ATTENTION


# -- 11/12/13/14. thread read ---------------------------------------------


def test_conversation_is_returned_chronologically():
    first = message("m-1", "Renter", "first", "2026-09-01T09:00:00")
    second = message("m-2", "Owner", "second", "2026-09-01T10:00:00")
    third = message("m-3", "Renter", "third", "2026-09-01T11:00:00")

    # Built newest-first, exactly as upstream returns it.
    fake = one_thread_fake([first, second, third])

    conversation = fake.inbox().get_conversation(conversation_ref_for(1001))

    assert [m["message"] for m in conversation["messages"]] == [
        "first",
        "second",
        "third",
    ]


def test_message_sanitization_exposes_only_the_agreed_fields():
    fake = one_thread_fake([GUEST_QUESTION])

    row = fake.inbox().get_conversation(conversation_ref_for(1001))["messages"][0]

    assert set(row) == {
        "message_ref",
        "sender",
        "subject",
        "message",
        "created_at",
        "message_status",
    }

    # route is never emitted: it cannot support a delivery claim.
    assert "route" not in row


def test_guest_contact_data_is_discarded_from_the_thread():
    fake = one_thread_fake([GUEST_QUESTION])

    body = json.dumps(fake.inbox().get_conversation(conversation_ref_for(1001)))

    assert "fixture.guest@example.invalid" not in body
    assert "Fixture Guest" not in body
    assert "guest_email" not in body
    assert "guest_name" not in body


def test_html_message_bodies_become_plain_text():
    html_message = message(
        "m-html",
        "Owner",
        "<p>Parking is free&nbsp;unless you want the driveway.</p>",
        "2026-09-01T10:00:00",
    )

    fake = one_thread_fake([html_message])

    body = fake.inbox().get_conversation(conversation_ref_for(1001))["messages"][0]

    assert "<p>" not in body["message"]
    assert "&nbsp;" not in body["message"]
    assert "Parking is free" in body["message"]


def test_plain_text_handles_non_strings():
    assert plain_text(None) == ""
    assert plain_text(42) == ""


def test_unknown_message_status_is_normalised_away():
    odd = message(
        "m-odd",
        "Owner",
        "hello",
        "2026-09-01T10:00:00",
        message_status="SomethingNew",
    )

    fake = one_thread_fake([odd])

    row = fake.inbox().get_conversation(conversation_ref_for(1001))["messages"][0]

    assert row["message_status"] is None


def test_a_property_outside_configuration_still_lists_without_a_slug():
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A, property_id=999999)],
        threads={THREAD_A: thread(THREAD_A, [GUEST_QUESTION])},
    )

    row = fake.inbox().list_conversations()[0]

    assert row["property_slug"] is None
    assert row["property_name"] is None


# -- tool layer ------------------------------------------------------------


def test_list_tool_fails_closed_when_the_provider_is_unavailable():
    fake = FakeLodgify(bookings_status=500)

    result = fake.tools().list_recent_guest_conversations()

    assert result["ok"] is False
    assert result["status"] == "unknown"
    assert "conversations" not in result


def test_get_tool_attaches_reply_guidance():
    fake = one_thread_fake([GUEST_QUESTION])

    result = fake.tools().get_guest_conversation(conversation_ref_for(1001))

    assert result["ok"] is True

    guidance = result["reply_guidance"]

    assert "acknowledgement" in guidance
    assert any(rule["topic"] == "parking" for rule in guidance["rules"])
    assert "refund and cancellation policy" in guidance["do_not_answer_from_memory"]


def test_the_api_key_never_appears_in_a_tool_result():
    fake = one_thread_fake([GUEST_QUESTION])

    tools = fake.tools()

    listed = json.dumps(tools.list_recent_guest_conversations())
    detail = json.dumps(tools.get_guest_conversation(conversation_ref_for(1001)))

    assert FAKE_KEY not in listed
    assert FAKE_KEY not in detail


def test_the_client_never_stores_the_credential_on_the_instance():
    client = LodgifyMessagingClient(api_key_provider=lambda: FAKE_KEY)

    assert FAKE_KEY not in json.dumps(
        {key: str(value) for key, value in vars(client).items()}
    )


def test_inbox_holds_no_provider_state_between_calls():
    fake = one_thread_fake([GUEST_QUESTION])

    inbox = fake.inbox()

    inbox.list_conversations()

    assert not any(
        isinstance(value, (dict, list)) and value for value in vars(inbox).values()
    )


def test_the_connector_never_issues_a_write_during_reads():
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        threads={THREAD_A: thread(THREAD_A, [GUEST_QUESTION])},
    )

    inbox = fake.inbox()

    inbox.list_conversations()
    inbox.get_conversation(conversation_ref_for(1001))

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)


def test_inbox_requires_a_messaging_client():
    with pytest.raises(TypeError):
        LodgifyInbox()  # type: ignore[call-arg]
