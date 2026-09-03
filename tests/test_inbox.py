"""Inbox reads: discovery, sanitization, ordering and the needs-attention rule.

No test reaches Lodgify. Everything runs through httpx.MockTransport.
"""

import json

import httpx
import pytest

from app.connectors.lodgify.inbox import (
    BOOKING_SCAN_SIZE,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    LodgifyInbox,
    classify_conversation,
    plain_text,
    read_messages,
)
from app.connectors.lodgify.messaging_client import (
    INBOX_STAY_FILTERS,
    STAY_FILTER_ALL,
    LodgifyMessagingClient,
)
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


def test_limit_bounds_the_response_not_the_scan():
    """`limit` bounds what is returned, not what is examined.

    This assertion was inverted until 2026-09-03: the Inbox read only `limit`
    threads, which meant it chose what to show before it knew what was recent.
    Deciding recency requires reading every candidate thread, because a booking
    row carries no last-message timestamp. The cost is real and deliberate.
    """
    fake = FakeLodgify(
        bookings=[booking(1000 + n, f"thread-{n}") for n in range(10)],
        threads={
            f"thread-{n}": thread(f"thread-{n}", [GUEST_QUESTION]) for n in range(10)
        },
    )

    rows = fake.inbox().list_conversations(limit=3)

    assert len(rows) == 3
    # Every candidate is read; only the response is cut to the limit.
    assert len(fake.thread_reads) == 10


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


# -- 12. discovery: the Inbox is an activity view, not a booking view -------
#
# The bug these cover: the Inbox listed the first page of bookings, cut it to
# `limit`, and only then read threads. Lodgify's booking list is not ordered by
# message activity -- verified live, it is ordered by neither `created_at` nor
# `updated_at` -- so a new message on an older or lower-listed booking was
# never read at all and could not appear. Discovery must scan the archive and
# order by the newest message in each thread, with the limit applied last.


def activity_fake(entries, **kwargs):
    """Bookings whose threads each end at a chosen time.

    `entries` is (booking_id, thread_uid, last_message_at, sender). All text is
    invented; none of it is real guest data.
    """
    bookings = []
    threads = {}

    for booking_id, uid, created_at, sender in entries:
        bookings.append(booking(booking_id, uid))
        threads[uid] = thread(
            uid,
            [message(f"m-{uid}", sender, "Invented fixture text.", created_at)],
        )

    return FakeLodgify(bookings=bookings, threads=threads, **kwargs)


def filler(count, start=2000, at="2026-01-01T09:00:00"):
    """Quiet older conversations, used to push a booking onto a later page."""
    return [(start + n, f"quiet-{n}", at, "Owner") for n in range(count)]


def refs(rows):
    return [row["conversation_ref"] for row in rows]


def test_a_recent_message_on_page_one_is_listed():
    fake = activity_fake(
        [
            (1001, "thread-new", "2026-09-03T14:00:00", "Renter"),
            (1002, "thread-old", "2026-01-04T09:00:00", "Owner"),
        ]
    )

    rows = fake.inbox().list_conversations()

    assert refs(rows)[0] == conversation_ref_for(1001)


def test_a_new_message_on_an_old_booking_beyond_page_one_reaches_the_top():
    """The live failure, reproduced with invented data.

    The booking sits past the first scanned page and is listed last, so booking
    order gives it no chance of being read. Its thread is the newest in the
    account, so it must come first.
    """
    entries = filler(BOOKING_SCAN_SIZE + 40) + [
        (9001, "thread-today", "2026-09-03T14:27:00", "Renter"),
    ]

    fake = activity_fake(entries)

    rows = fake.inbox().list_conversations()

    assert refs(rows)[0] == conversation_ref_for(9001)
    assert rows[0]["status"] == ConversationStatus.NEEDS_ATTENTION.value


def test_every_booking_page_is_scanned():
    fake = activity_fake(filler(BOOKING_SCAN_SIZE + 40))

    fake.inbox().list_conversations()

    # One page sequence per stay filter the Inbox enumerates.
    pages = [request.url.params.get("page") for request in fake.booking_reads]

    assert pages == ["1", "2"] * len(INBOX_STAY_FILTERS)


def test_ordering_is_by_the_newest_message_in_each_thread():
    fake = activity_fake(
        [
            (1001, "thread-a", "2026-09-01T10:00:00", "Owner"),
            (1002, "thread-b", "2026-09-03T10:00:00", "Renter"),
            (1003, "thread-c", "2026-09-02T10:00:00", "Owner"),
        ]
    )

    rows = fake.inbox().list_conversations()

    assert refs(rows) == [
        conversation_ref_for(1002),
        conversation_ref_for(1003),
        conversation_ref_for(1001),
    ]


def test_booking_list_order_does_not_decide_inbox_order():
    """Live evidence: the booking list is ordered by neither created nor
    updated time, so its order carries no information about message activity."""
    fake = activity_fake(
        [
            (1001, "thread-a", "2026-05-01T10:00:00", "Owner"),
            (1002, "thread-b", "2026-09-03T10:00:00", "Renter"),
        ]
    )

    rows = fake.inbox().list_conversations()

    assert refs(rows) != [conversation_ref_for(1001), conversation_ref_for(1002)]


def test_the_limit_is_applied_after_activity_ordering():
    """The newest conversation is listed last by the provider. A limit applied
    to booking order would drop it; applied after ordering, it survives."""
    entries = filler(BOOKING_SCAN_SIZE + 40) + [
        (9001, "thread-today", "2026-09-03T14:27:00", "Renter"),
    ]

    rows = activity_fake(entries).inbox().list_conversations(limit=3)

    assert len(rows) == 3
    assert refs(rows)[0] == conversation_ref_for(9001)


def test_a_conversation_is_never_listed_twice():
    """A booking repeated across pages must not become two inbox rows."""
    repeated = booking(1001, "thread-a")

    fake = FakeLodgify(
        bookings=[repeated, booking(1002, "thread-b"), repeated],
        threads={
            "thread-a": thread("thread-a", [GUEST_QUESTION]),
            "thread-b": thread("thread-b", [OWNER_ANSWER]),
        },
    )

    listed = refs(fake.inbox().list_conversations())

    assert len(listed) == len(set(listed))
    assert listed.count(conversation_ref_for(1001)) == 1


def test_pagination_stops_on_an_empty_page():
    """A page exactly filling the scan size must not spin the scan forever."""
    fake = activity_fake(filler(BOOKING_SCAN_SIZE))

    rows = fake.inbox().list_conversations(limit=MAX_LIMIT)

    # Page one is full, so page two is requested and comes back empty; that
    # terminates the scan for each filter rather than spinning to the cap.
    assert len(fake.booking_reads) == 2 * len(INBOX_STAY_FILTERS)
    assert len(rows) == MAX_LIMIT


def test_responded_and_needs_attention_are_ordered_together_by_activity():
    fake = activity_fake(
        [
            (1001, "thread-guest-old", "2026-09-01T10:00:00", "Renter"),
            (1002, "thread-owner-new", "2026-09-03T10:00:00", "Owner"),
        ]
    )

    rows = fake.inbox().list_conversations()

    assert refs(rows) == [conversation_ref_for(1002), conversation_ref_for(1001)]
    assert rows[0]["status"] == ConversationStatus.RESPONDED.value
    assert rows[1]["status"] == ConversationStatus.NEEDS_ATTENTION.value


def test_refresh_re_reads_the_provider_every_time():
    """Nothing caches. A second call must issue a second set of live reads."""
    fake = activity_fake(
        [
            (1001, "thread-a", "2026-09-01T10:00:00", "Owner"),
            (1002, "thread-b", "2026-09-02T10:00:00", "Renter"),
        ]
    )

    inbox = fake.inbox()

    inbox.list_conversations()
    first_bookings = len(fake.booking_reads)
    first_threads = len(fake.thread_reads)

    inbox.list_conversations()

    assert len(fake.booking_reads) == first_bookings * 2
    assert len(fake.thread_reads) == first_threads * 2


def test_one_unreadable_thread_does_not_corrupt_the_inbox():
    """Partial results are the existing contract: an unreadable thread becomes
    UNKNOWN rather than taking the whole Inbox down with it."""
    fake = activity_fake(
        [
            (1001, "thread-a", "2026-09-01T10:00:00", "Owner"),
            (1002, "thread-b", "2026-09-03T10:00:00", "Renter"),
        ]
    )

    # thread-c has no scripted payload and answers 404 -> unavailable.
    fake.bookings.append(booking(1003, "thread-missing"))

    def handler(request):
        if request.url.path == "/v2/messaging/thread-missing":
            fake.requests.append(request)
            return httpx.Response(503, json={})

        return fake.handler(request)

    inbox = LodgifyInbox(
        LodgifyMessagingClient(
            api_key_provider=lambda: FAKE_KEY,
            transport=httpx.MockTransport(handler),
        )
    )

    rows = inbox.list_conversations()

    listed = refs(rows)

    assert conversation_ref_for(1002) in listed
    assert conversation_ref_for(1001) in listed
    assert conversation_ref_for(1003) in listed

    unreadable = next(
        row for row in rows if row["conversation_ref"] == conversation_ref_for(1003)
    )

    assert unreadable["status"] == ConversationStatus.UNKNOWN.value
    # Unknown sorts to the bottom; it never displaces real activity.
    assert listed[0] == conversation_ref_for(1002)


# -- 13. discovery must ask for every stay ---------------------------------
#
# The second live failure, and a different bug from the ordering one. Lodgify's
# booking list defaults to `stayFilter=Upcoming`. Measured live 2026-09-03:
# Upcoming 145, Current 12, Historic 908, All 1062. AgentGuard sent no filter,
# so it saw only upcoming reservations -- and a guest asking to check out late
# is a *current* stay, in the property right now. No amount of ordering can
# surface a conversation whose booking is never enumerated.


def test_booking_discovery_names_its_stay_filters_explicitly():
    """Never send this endpoint an unfiltered request: it defaults to
    `Upcoming`, which hid every current and past stay."""
    fake = activity_fake([(1001, "thread-a", "2026-09-03T12:06:00", "Renter")])

    fake.inbox().list_conversations()

    sent = [request.url.params.get("stayFilter") for request in fake.booking_reads]

    assert sent
    assert set(sent) == set(INBOX_STAY_FILTERS)
    assert "Current" in sent


def test_a_single_booking_lookup_searches_every_stay():
    """Resolution is a different question from the Inbox listing: a webhook or
    a stored ref can name a booking from any period, so it searches them all."""
    fake = activity_fake([(1001, "thread-a", "2026-09-03T12:06:00", "Renter")])

    fake.inbox().resolve(conversation_ref_for(1001))

    sent = [request.url.params.get("stayFilter") for request in fake.booking_reads]

    assert sent and all(value == STAY_FILTER_ALL for value in sent)


def test_a_current_stay_is_discoverable():
    """A guest mid-stay messaging today must reach the Inbox.

    The provider only returns this row when asked for every stay, so the fake
    withholds it under the default filter exactly as Lodgify does.
    """
    upcoming = booking(1001, "thread-upcoming")
    current = booking(9001, "thread-current")

    class StayFilterFake(FakeLodgify):
        def handler(self, request):
            if request.url.path == "/v2/reservations/bookings":
                asked = request.url.params.get("stayFilter")
                # The provider only yields the in-progress stay when asked
                # for it by name, exactly as Lodgify does.
                self.bookings = [current] if asked == "Current" else [upcoming]

            return super().handler(request)

    fake = StayFilterFake(
        bookings=[upcoming],
        threads={
            "thread-upcoming": thread(
                "thread-upcoming",
                [message("m-up", "Owner", "Invented.", "2026-08-01T09:00:00")],
            ),
            "thread-current": thread(
                "thread-current",
                [message("m-cur", "Renter", "Invented.", "2026-09-03T12:06:33")],
            ),
        },
    )

    rows = fake.inbox().list_conversations()

    assert refs(rows)[0] == conversation_ref_for(9001)
    assert rows[0]["status"] == ConversationStatus.NEEDS_ATTENTION.value


def test_two_bookings_sharing_a_thread_produce_one_row():
    """12 bookings in the live account share a thread with another booking.

    The Inbox lists conversations, so one thread is one row however many
    reservations point at it.
    """
    fake = FakeLodgify(
        bookings=[booking(1001, "thread-shared"), booking(1002, "thread-shared")],
        threads={"thread-shared": thread("thread-shared", [GUEST_QUESTION])},
    )

    rows = fake.inbox().list_conversations()

    assert len(rows) == 1
    assert len(fake.thread_reads) == 1


def test_a_conversation_on_a_deep_page_is_still_discovered():
    """The live conversation sat at index 1044, on page 11."""
    entries = filler(BOOKING_SCAN_SIZE * 10 + 44) + [
        (9001, "thread-deep", "2026-09-03T12:06:33", "Renter"),
    ]

    rows = activity_fake(entries).inbox().list_conversations(limit=5)

    assert refs(rows)[0] == conversation_ref_for(9001)
