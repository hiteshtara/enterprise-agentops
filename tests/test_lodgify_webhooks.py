"""Webhook signature verification and safe event handling.

The endpoint is a URL anyone can POST to, so most of these tests are about what
it refuses. No test reaches Lodgify or OpenAI; every payload is invented.

The `guest_name` and `message` values below are fictional on purpose: the real
payload carries both, and the point of several of these tests is that neither
survives contact with our code.
"""

import hashlib
import hmac
import json

import pytest

from app.connectors.lodgify.refs import conversation_ref_for
from app.lodgify_webhooks import (
    GUEST_MESSAGE_RECEIVED,
    KNOWN_EVENTS,
    WebhookLog,
    handle_event,
)
from app.webhook_security import (
    SIGNATURE_HEADER,
    WebhookNotConfigured,
    is_configured,
    resolve_webhook_secret,
    signature_matches,
)
from tests.lodgify_fakes import THREAD_A, FakeLodgify, booking

SECRET = "test-only-webhook-signing-secret-not-real"

OTHER_SECRET = "a-different-secret-entirely"

GUEST_MESSAGE_PAYLOAD = {
    "action": "guest_message_received",
    "thread_uid": THREAD_A,
    "message_id": 12345678,
    "inbox_uid": "B12345",
    "guest_name": "Fictional Person",
    "subject": None,
    "message": "Invented message content for this test.",
    "creation_time": "2026-09-03T14:15:00+00:00",
    "has_attachments": False,
    "sub_owner_id": 12345,
}


def sign(body: bytes, secret: str = SECRET) -> str:
    return (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )


def body_of(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


# -- signature verification ------------------------------------------------


def test_a_valid_signature_is_accepted():
    body = body_of(GUEST_MESSAGE_PAYLOAD)

    assert signature_matches(body, sign(body), SECRET) is True


def test_a_signature_from_another_secret_is_rejected():
    body = body_of(GUEST_MESSAGE_PAYLOAD)

    assert signature_matches(body, sign(body, OTHER_SECRET), SECRET) is False


@pytest.mark.parametrize("header", [None, "", "   ", "sha256=", "not-a-signature", 42])
def test_a_missing_or_malformed_signature_is_rejected(header):
    assert signature_matches(b"{}", header, SECRET) is False


def test_the_signature_covers_the_exact_bytes_received():
    """Re-serialising the JSON changes the bytes, and must break the signature.

    This is why the route hashes the raw body rather than anything parsed.
    """
    original = b'{"action":"rate_change","a":1}'
    signature = sign(original)

    # Same object, different key order and whitespace.
    reserialised = json.dumps(json.loads(original), indent=2).encode("utf-8")

    assert signature_matches(original, signature, SECRET) is True
    assert signature_matches(reserialised, signature, SECRET) is False


def test_an_uppercase_hex_signature_still_verifies():
    body = body_of(GUEST_MESSAGE_PAYLOAD)

    assert signature_matches(body, sign(body).upper(), SECRET) is True


def test_a_bare_digest_without_the_prefix_verifies():
    body = body_of(GUEST_MESSAGE_PAYLOAD)

    digest = sign(body).removeprefix("sha256=")

    assert signature_matches(body, digest, SECRET) is True


def test_verification_uses_a_constant_time_comparison():
    import inspect

    from app import webhook_security

    source = inspect.getsource(webhook_security.signature_matches)

    # A byte-by-byte `==` leaks how much of a forged signature was correct.
    assert "compare_digest" in source


def test_the_webhook_secret_is_its_own_credential(monkeypatch):
    monkeypatch.delenv("LODGIFY_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("LODGIFY_API_KEY", "an-api-key-that-is-not-a-webhook-secret")

    assert is_configured() is False

    with pytest.raises(WebhookNotConfigured):
        resolve_webhook_secret()

    monkeypatch.setenv("LODGIFY_WEBHOOK_SECRET", SECRET)

    assert is_configured() is True
    assert resolve_webhook_secret() == SECRET


# -- event handling --------------------------------------------------------


def inbox_with_thread():
    return FakeLodgify(bookings=[booking(1001, THREAD_A)]).inbox()


def test_the_documented_event_enum_is_recorded():
    # Read from Lodgify's own subscribe endpoint documentation.
    assert len(KNOWN_EVENTS) == 10
    assert GUEST_MESSAGE_RECEIVED == "guest_message_received"
    assert "booking_change" in KNOWN_EVENTS
    assert "rate_change" in KNOWN_EVENTS


def test_a_guest_message_event_resolves_our_own_conversation_reference():
    receipt = handle_event(GUEST_MESSAGE_PAYLOAD, inbox_with_thread())

    assert receipt.event_type == GUEST_MESSAGE_RECEIVED
    assert receipt.resolved is True
    assert receipt.conversation_ref == conversation_ref_for(1001)


def test_the_receipt_carries_no_guest_data():
    receipt = handle_event(GUEST_MESSAGE_PAYLOAD, inbox_with_thread())

    body = json.dumps(receipt.to_dict())

    assert "Fictional Person" not in body
    assert "Invented message content" not in body
    assert THREAD_A not in body
    assert "12345678" not in body
    assert "B12345" not in body


def test_a_thread_we_do_not_know_resolves_to_nothing():
    payload = {
        **GUEST_MESSAGE_PAYLOAD,
        "thread_uid": "99999999-9999-4999-8999-999999999999",
    }

    receipt = handle_event(payload, inbox_with_thread())

    assert receipt.resolved is False
    assert receipt.conversation_ref is None


def test_a_known_event_with_no_handler_is_acknowledged():
    receipt = handle_event({"action": "rate_change"}, inbox_with_thread())

    assert receipt.known_event is True
    assert receipt.resolved is False
    assert "no handler" in receipt.detail


def test_an_unknown_event_is_acknowledged_and_ignored():
    receipt = handle_event({"action": "something_new"}, inbox_with_thread())

    assert receipt.event_type == "something_new"
    assert receipt.known_event is False
    assert receipt.conversation_ref is None


@pytest.mark.parametrize("payload", [None, "a string", 42, {}, {"action": ""}])
def test_a_malformed_payload_does_not_raise(payload):
    receipt = handle_event(payload, inbox_with_thread())

    assert receipt.resolved is False


def test_a_provider_read_failure_is_acknowledged_not_invented():
    broken = FakeLodgify(bookings_status=503).inbox()

    receipt = handle_event(GUEST_MESSAGE_PAYLOAD, broken)

    assert receipt.resolved is False
    assert receipt.conversation_ref is None
    assert "could not be read" in receipt.detail


def test_the_handler_works_without_a_connector():
    receipt = handle_event(GUEST_MESSAGE_PAYLOAD, None)

    assert receipt.resolved is False
    assert "not configured" in receipt.detail


def test_a_repeated_event_produces_the_same_result():
    inbox = inbox_with_thread()

    first = handle_event(GUEST_MESSAGE_PAYLOAD, inbox, now="2026-09-03T10:00:00")
    second = handle_event(GUEST_MESSAGE_PAYLOAD, inbox, now="2026-09-03T10:00:01")

    # Retries re-check current state. Nothing accumulates.
    assert first.conversation_ref == second.conversation_ref
    assert first.resolved == second.resolved


def test_handling_an_event_never_writes_to_lodgify():
    fake = FakeLodgify(bookings=[booking(1001, THREAD_A)])

    handle_event(GUEST_MESSAGE_PAYLOAD, fake.inbox())

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)


def test_the_event_log_holds_only_safe_projections():
    log = WebhookLog()

    log.record(handle_event(GUEST_MESSAGE_PAYLOAD, inbox_with_thread()))

    body = json.dumps(log.recent())

    assert "Fictional Person" not in body
    assert "Invented message content" not in body
    assert set(log.recent()[0]) == {
        "event_type",
        "known_event",
        "received_at",
        "conversation_ref",
        "resolved",
        "detail",
    }


# -- the HTTP endpoint -----------------------------------------------------


@pytest.fixture
def webhook_api(api, monkeypatch):
    monkeypatch.setenv("LODGIFY_WEBHOOK_SECRET", SECRET)

    fake = FakeLodgify(bookings=[booking(1001, THREAD_A)])

    api.module.lodgify_inbox = fake.inbox()
    api.module.webhook_log.clear()
    api.fake = fake

    return api


def post_event(client, payload, signature=None, secret=SECRET):
    body = body_of(payload)

    return client.post(
        "/webhooks/lodgify",
        content=body,
        headers={
            "content-type": "application/json",
            SIGNATURE_HEADER: signature
            if signature is not None
            else sign(body, secret),
        },
    )


def test_a_signed_event_is_accepted(webhook_api):
    response = post_event(webhook_api.anonymous(), GUEST_MESSAGE_PAYLOAD)

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["received"] is True
    assert payload["event_type"] == GUEST_MESSAGE_RECEIVED
    assert payload["conversation_ref"] == conversation_ref_for(1001)


def test_an_unsigned_event_is_refused(webhook_api):
    body = body_of(GUEST_MESSAGE_PAYLOAD)

    response = webhook_api.anonymous().post(
        "/webhooks/lodgify",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 401
    # Nothing was looked up on the strength of an unverified event.
    assert webhook_api.fake.requests == []


def test_a_forged_signature_is_refused(webhook_api):
    response = post_event(
        webhook_api.anonymous(), GUEST_MESSAGE_PAYLOAD, secret=OTHER_SECRET
    )

    assert response.status_code == 401
    assert webhook_api.fake.requests == []


def test_a_tampered_body_is_refused(webhook_api):
    good = body_of(GUEST_MESSAGE_PAYLOAD)
    signature = sign(good)

    tampered = body_of({**GUEST_MESSAGE_PAYLOAD, "thread_uid": "someone-elses-thread"})

    response = webhook_api.anonymous().post(
        "/webhooks/lodgify",
        content=tampered,
        headers={"content-type": "application/json", SIGNATURE_HEADER: signature},
    )

    assert response.status_code == 401


def test_the_endpoint_refuses_everything_when_unconfigured(api, monkeypatch):
    monkeypatch.delenv("LODGIFY_WEBHOOK_SECRET", raising=False)

    response = post_event(api.anonymous(), GUEST_MESSAGE_PAYLOAD)

    # Better to refuse than to accept an unverified event.
    assert response.status_code == 503


def test_the_endpoint_never_sends_a_guest_message(webhook_api):
    post_event(webhook_api.anonymous(), GUEST_MESSAGE_PAYLOAD)

    assert webhook_api.fake.posts == []


def test_a_duplicate_delivery_is_harmless(webhook_api):
    client = webhook_api.anonymous()

    first = post_event(client, GUEST_MESSAGE_PAYLOAD)
    second = post_event(client, GUEST_MESSAGE_PAYLOAD)

    assert first.status_code == second.status_code == 200
    assert first.json()["conversation_ref"] == second.json()["conversation_ref"]
    assert webhook_api.fake.posts == []


def test_the_response_leaks_no_guest_data_or_secret(webhook_api):
    response = post_event(webhook_api.anonymous(), GUEST_MESSAGE_PAYLOAD)

    body = response.text

    assert "Fictional Person" not in body
    assert "Invented message content" not in body
    assert THREAD_A not in body
    assert SECRET not in body


def test_an_unknown_event_still_returns_200(webhook_api):
    # Lodgify retries a non-200 up to ten times; retrying will not make us
    # understand an event we have no handler for.
    response = post_event(webhook_api.anonymous(), {"action": "brand_new_event"})

    assert response.status_code == 200
    assert response.json()["known_event"] is False


def test_recent_events_require_admin(webhook_api):
    post_event(webhook_api.anonymous(), GUEST_MESSAGE_PAYLOAD)

    assert webhook_api.anonymous().get("/webhooks/lodgify/recent").status_code == 401
    assert (
        webhook_api.client("APPROVER").get("/webhooks/lodgify/recent").status_code
        == 403
    )

    events = webhook_api.client("ADMIN").get("/webhooks/lodgify/recent").json()

    assert len(events) == 1
    assert events[0]["event_type"] == GUEST_MESSAGE_RECEIVED


def test_the_endpoint_works_without_openai(webhook_api, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert post_event(webhook_api.anonymous(), GUEST_MESSAGE_PAYLOAD).status_code == 200


# -- the shape Lodgify actually sends ---------------------------------------
#
# Verified live 2026-09-03: the delivery is a JSON *array* wrapping the event
# object, while the published example shows a bare object. Both must work --
# the receiver understood only the documented shape at first and dropped every
# real event with a 200.


def test_the_live_array_wrapped_delivery_is_understood():
    receipt = handle_event([GUEST_MESSAGE_PAYLOAD], inbox_with_thread())

    assert receipt.event_type == GUEST_MESSAGE_RECEIVED
    assert receipt.resolved is True
    assert receipt.conversation_ref == conversation_ref_for(1001)


def test_the_documented_bare_object_still_works():
    receipt = handle_event(GUEST_MESSAGE_PAYLOAD, inbox_with_thread())

    assert receipt.event_type == GUEST_MESSAGE_RECEIVED
    assert receipt.resolved is True


def test_an_array_wrapped_event_leaks_no_guest_data():
    receipt = handle_event([GUEST_MESSAGE_PAYLOAD], inbox_with_thread())

    body = json.dumps(receipt.to_dict())

    assert "Fictional Person" not in body
    assert "Invented message content" not in body


def test_an_empty_or_junk_array_is_not_an_event():
    assert handle_event([], inbox_with_thread()).event_type == "unparsable"
    assert handle_event(["nope"], inbox_with_thread()).event_type == "unparsable"


def test_an_array_wrapped_event_is_accepted_over_http(webhook_api):
    response = post_event(webhook_api.anonymous(), [GUEST_MESSAGE_PAYLOAD])

    assert response.status_code == 200
    assert response.json()["event_type"] == GUEST_MESSAGE_RECEIVED
    assert response.json()["resolved"] is True
