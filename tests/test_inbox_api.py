"""The Inbox HTTP surface and the governed send end to end.

Covers the properties that only exist once the route, the registry, the approval
store and RBAC are wired together: that submitting a reply sends nothing, that
only an authorised approver can release it, that rejection sends nothing, and
that the audit trail records the exact approved text and no provider identifier.
"""

import json

import pytest

from app.connectors.lodgify.refs import conversation_ref_for
from app.migration_store import MigrationBatchStore
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

GUEST = message(
    "m-guest-1",
    "Renter",
    "Is there parking?",
    "2026-09-01T10:00:00",
    message_status=None,
    route=None,
)

SENT = message(
    "m-sent-1",
    "Owner",
    BODY,
    "2026-09-02T12:00:00",
    subject=SUBJECT,
    message_status="Delivered",
    route=None,
)


@pytest.fixture
def inbox_api(api):
    """The reloaded app with a scripted Lodgify behind the Inbox.

    The connector is unconfigured in tests, so the module builds no inbox. The
    fake is installed afterwards and the registry rebuilt around it -- the
    routes read both from module scope at call time.
    """
    module = api.module

    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        # Submitting a reply reaches Lodgify not at all -- the tool parks for
        # approval before its function runs. So the two scripted reads are the
        # pre-send snapshot and the post-send verification.
        thread_sequence={
            THREAD_A: [
                thread(THREAD_A, [GUEST]),
                thread(THREAD_A, [GUEST, SENT]),
            ]
        },
        threads={THREAD_A: thread(THREAD_A, [GUEST])},
    )

    module.lodgify_inbox = fake.inbox()
    module.tool_registry = build_tool_registry(
        migration_store=MigrationBatchStore(database=module.database),
        lodgify_messaging=fake.tools(),
    )
    module.agent.tool_registry = module.tool_registry

    api.fake = fake

    return api


# -- reads -----------------------------------------------------------------


def test_inbox_lists_sanitized_conversations(inbox_api):
    response = inbox_api.client("ADMIN").get("/inbox")

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["count"] == 1

    row = payload["conversations"][0]

    assert row["conversation_ref"] == REF
    assert row["status"] == "needs_attention"
    assert (
        row["property_slug"] == "renovated-3rd-floor-retreat-3-beds-roslindale-village"
    )

    body = response.text

    assert "fixture.guest@example.invalid" not in body
    assert THREAD_A not in body
    assert "1001" not in body


def test_conversation_detail_is_chronological(inbox_api):
    response = inbox_api.client("ADMIN").get(f"/inbox/{REF}")

    assert response.status_code == 200, response.text

    payload = response.json()

    assert [m["sender"] for m in payload["messages"]] == ["Renter"]
    assert payload["messages"][0]["message"] == "Is there parking?"


def test_unknown_conversation_is_404(inbox_api):
    response = inbox_api.client("ADMIN").get("/inbox/PH-ZZZZZZZZ")

    assert response.status_code == 404


def test_inbox_requires_authentication(inbox_api):
    assert inbox_api.anonymous().get("/inbox").status_code == 401
    assert inbox_api.anonymous().get(f"/inbox/{REF}").status_code == 401


def test_inbox_limit_is_bounded_by_the_route(inbox_api):
    assert inbox_api.client("ADMIN").get("/inbox?limit=0").status_code == 422
    assert inbox_api.client("ADMIN").get("/inbox?limit=101").status_code == 422


def test_inbox_is_unavailable_without_a_connector(api):
    api.module.lodgify_inbox = None

    assert api.client("ADMIN").get("/inbox").status_code == 503


# -- submitting a reply sends nothing --------------------------------------


def test_submitting_a_reply_parks_for_approval_and_sends_nothing(inbox_api):
    response = inbox_api.client("ADMIN").post(
        f"/inbox/{REF}/reply",
        json={"subject": SUBJECT, "message": BODY},
    )

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["status"] == "WAITING_FOR_APPROVAL"

    approval = payload["approval_required"]

    assert approval["tool"] == "send_guest_reply"
    assert approval["risk"] == "DANGEROUS"

    # The approval carries the exact text a human will read and approve.
    assert approval["arguments"] == {
        "conversation_ref": REF,
        "subject": SUBJECT,
        "message": BODY,
    }

    # Nothing reached Lodgify.
    assert inbox_api.fake.posts == []


def test_submitting_a_reply_requires_run_agent(inbox_api):
    response = inbox_api.client("VIEWER").post(
        f"/inbox/{REF}/reply",
        json={"subject": SUBJECT, "message": BODY},
    )

    assert response.status_code == 403
    assert inbox_api.fake.posts == []


def test_empty_reply_is_rejected_by_the_route(inbox_api):
    response = inbox_api.client("ADMIN").post(
        f"/inbox/{REF}/reply",
        json={"subject": "", "message": ""},
    )

    assert response.status_code == 422
    assert inbox_api.fake.posts == []


# -- approval --------------------------------------------------------------


def submit(client, api):
    response = client.post(
        f"/inbox/{REF}/reply",
        json={"subject": SUBJECT, "message": BODY},
    )

    assert response.status_code == 200, response.text

    return response.json()["approval_required"]["approval_id"]


def test_admin_approval_sends_exactly_once_and_confirms(inbox_api):
    client = inbox_api.client("ADMIN")

    approval_id = submit(client, inbox_api)

    response = client.post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["approved"] is True
    assert payload["run_status"] == "COMPLETED"
    assert payload["result"]["status"] == "confirmed_sent"
    assert payload["result"]["message"] == "Lodgify reports the message as Delivered."

    # Exactly one POST for one approval.
    assert len(inbox_api.fake.posts) == 1

    body = json.loads(inbox_api.fake.posts[0].content)

    assert body[0]["message"] == BODY
    assert body[0]["type"] == "Owner"
    assert body[0]["send_notification"] is True


def test_an_approval_cannot_be_used_twice(inbox_api):
    client = inbox_api.client("ADMIN")

    approval_id = submit(client, inbox_api)

    first = client.post(f"/agent/approvals/{approval_id}", json={"approved": True})
    second = client.post(f"/agent/approvals/{approval_id}", json={"approved": True})

    assert first.status_code == 200
    assert second.status_code == 404

    # The second attempt sent nothing.
    assert len(inbox_api.fake.posts) == 1


def test_approver_cannot_release_a_dangerous_send(inbox_api):
    approval_id = submit(inbox_api.client("ADMIN"), inbox_api)

    response = inbox_api.client("APPROVER").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    assert response.status_code == 403
    assert inbox_api.fake.posts == []


def test_rejection_sends_nothing(inbox_api):
    client = inbox_api.client("ADMIN")

    approval_id = submit(client, inbox_api)

    response = client.post(
        f"/agent/approvals/{approval_id}",
        json={"approved": False},
    )

    assert response.status_code == 200, response.text
    assert response.json()["run_status"] == "CANCELLED"
    assert inbox_api.fake.posts == []


# -- audit -----------------------------------------------------------------


def test_audit_records_the_exact_approved_text_and_no_provider_identifier(inbox_api):
    client = inbox_api.client("ADMIN")

    approval_id = submit(client, inbox_api)

    client.post(f"/agent/approvals/{approval_id}", json={"approved": True})

    events = client.get("/audit/events?limit=100").json()

    types = {event["event_type"] for event in events}

    assert {
        "TOOL_REQUESTED",
        "APPROVAL_REQUIRED",
        "APPROVAL_GRANTED",
        "TOOL_EXECUTED",
    } <= types

    executed = next(e for e in events if e["event_type"] == "TOOL_EXECUTED")

    # The outbound text is deliberately audit-visible: it is the approved,
    # externally-visible action.
    assert executed["details"]["arguments"]["message"] == BODY
    assert executed["details"]["arguments"]["subject"] == SUBJECT
    assert executed["details"]["result"]["status"] == "confirmed_sent"
    assert executed["actor_user_id"]

    body = json.dumps(events)

    # ...but nothing else the provider gave us is.
    assert FAKE_KEY not in body
    assert THREAD_A not in body
    assert "fixture.guest@example.invalid" not in body
    assert "+15550000000" not in body
    assert "booking_id" not in body
    assert "thread_uid" not in body


def test_unauthorized_approval_is_audited_and_leaves_the_approval_pending(inbox_api):
    approval_id = submit(inbox_api.client("ADMIN"), inbox_api)

    inbox_api.client("APPROVER").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    events = (
        inbox_api.client("ADMIN")
        .get("/audit/events?event_type=AUTHORIZATION_DENIED")
        .json()
    )

    assert len(events) == 1
    assert events[0]["details"]["required_permission"] == "APPROVE_DANGEROUS"

    approvals = inbox_api.client("ADMIN").get("/approvals").json()

    assert approvals[0]["status"] == "PENDING"


# -- tools listing ---------------------------------------------------------


def test_console_sees_the_send_tool_as_dangerous(inbox_api):
    tools = inbox_api.client("ADMIN").get("/tools").json()

    send = next(tool for tool in tools if tool["name"] == "send_guest_reply")

    assert send["risk"] == "DANGEROUS"
    assert set(send["parameters"]["properties"]) == {
        "conversation_ref",
        "subject",
        "message",
    }
