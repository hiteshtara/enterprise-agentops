"""The Inbox HTTP surface and the governed send end to end.

Covers the properties that only exist once the route, the registry, the approval
store and RBAC are wired together: that submitting a reply sends nothing, that
only an authorised approver can release it, that rejection sends nothing, that
the audit trail records the exact approved text and no provider identifier, and
that a reply composed against a conversation state that has since moved on is
refused by the server rather than only by the console.
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

# Well-formed (passes `is_well_formed`) but matches no booking in the fixture
# archive -- what `LodgifyInbox.resolve` refuses with a `ValueError`.
UNKNOWN_REF = "PH-ZZZZZZZZ"

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

# The message that arrives *after* a draft was prepared, which is what moves the
# conversation on and therefore changes its fingerprint.
GUEST_AGAIN = message(
    "m-guest-2",
    "Renter",
    "Actually, ignore that -- can we drop a bag off early instead?",
    "2026-09-01T18:00:00",
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
        # One stable pre-send state, answered to every read. Submitting a reply
        # reaches Lodgify not at all -- the tool parks for approval before its
        # function runs -- but the route does now re-read the thread to decide
        # whether the submitted draft is still current, and the console reads it
        # to learn the fingerprint in the first place. Keeping the fallback
        # stable means a test may read as often as it likes; `arm_send` queues
        # the one pair of reads an approved send performs.
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


# -- helpers ---------------------------------------------------------------


def current_fingerprint(client) -> str:
    """The fingerprint the console would be holding for this conversation.

    Read through the API rather than computed here, so the value a test submits
    is exactly the value a browser would have been given.
    """
    response = client.get(f"/inbox/{REF}")

    assert response.status_code == 200, response.text

    fingerprint = response.json()["fingerprint"]

    assert fingerprint

    return fingerprint


def reply_payload(
    fingerprint: str | None,
    subject: str = SUBJECT,
    body: str = BODY,
) -> dict:
    """A reply submission. `None` omits the fingerprint entirely."""
    payload = {"subject": subject, "message": body}

    if fingerprint is not None:
        payload["conversation_fingerprint"] = fingerprint

    return payload


def guest_writes_again(api) -> None:
    """The conversation moves on under a prepared draft."""
    api.fake.threads[THREAD_A] = thread(THREAD_A, [GUEST, GUEST_AGAIN])


def arm_send(api) -> None:
    """Script the two thread reads one approved send performs.

    `send_reply` snapshots the thread, POSTs once, then re-reads to attribute
    the new row by difference. Queuing that pair here, immediately before the
    approval, keeps every earlier read -- the console's detail read, the
    staleness guard's -- on the stable pre-send state.
    """
    api.fake.thread_sequence[THREAD_A] = [
        thread(THREAD_A, [GUEST]),
        thread(THREAD_A, [GUEST, SENT]),
    ]


# -- submitting a reply sends nothing --------------------------------------


def test_submitting_a_reply_parks_for_approval_and_sends_nothing(inbox_api):
    client = inbox_api.client("ADMIN")

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(current_fingerprint(client)),
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
    fingerprint = current_fingerprint(inbox_api.client("ADMIN"))

    response = inbox_api.client("VIEWER").post(
        f"/inbox/{REF}/reply",
        json=reply_payload(fingerprint),
    )

    assert response.status_code == 403
    assert inbox_api.fake.posts == []


def test_empty_reply_is_rejected_by_the_route(inbox_api):
    client = inbox_api.client("ADMIN")

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(current_fingerprint(client), subject="", body=""),
    )

    assert response.status_code == 422
    assert inbox_api.fake.posts == []


# -- approval --------------------------------------------------------------


def submit(client, api):
    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(current_fingerprint(client)),
    )

    assert response.status_code == 200, response.text

    # Queue the pre-send snapshot and the post-send re-read only now, so the
    # reads the submission itself performed did not consume them.
    arm_send(api)

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


# -- the stale-draft guard --------------------------------------------------
#
# The console refuses to submit a STALE draft, but a UI check is convenience,
# not security. These cover the server-side guard: the route compares the
# fingerprint the submitter was looking at against the live conversation and
# refuses a submission composed against a state that has moved on.


def test_a_current_fingerprint_is_accepted_and_creates_one_approval(inbox_api):
    client = inbox_api.client("ADMIN")

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(current_fingerprint(client)),
    )

    assert response.status_code == 200, response.text

    approvals = inbox_api.module.approval_store.list_approvals()

    assert len(approvals) == 1
    assert approvals[0]["tool"] == "send_guest_reply"
    assert approvals[0]["risk"] == "DANGEROUS"
    assert approvals[0]["status"] == "PENDING"
    assert inbox_api.fake.posts == []


def test_a_stale_draft_is_refused_with_409(inbox_api):
    client = inbox_api.client("ADMIN")

    fingerprint = current_fingerprint(client)

    guest_writes_again(inbox_api)

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(fingerprint),
    )

    assert response.status_code == 409, response.text

    detail = response.json()["detail"]

    assert "Regenerate" in detail
    assert "prepared" in detail


def test_a_stale_submission_creates_no_approval_and_no_run(inbox_api):
    client = inbox_api.client("ADMIN")

    fingerprint = current_fingerprint(client)

    guest_writes_again(inbox_api)

    client.post(f"/inbox/{REF}/reply", json=reply_payload(fingerprint))

    assert inbox_api.module.approval_store.list_approvals() == []
    assert inbox_api.module.run_store.list_runs() == []


def test_a_stale_submission_executes_nothing_and_sends_nothing(inbox_api):
    client = inbox_api.client("ADMIN")

    fingerprint = current_fingerprint(client)

    guest_writes_again(inbox_api)

    client.post(f"/inbox/{REF}/reply", json=reply_payload(fingerprint))

    # No POST reached the provider...
    assert inbox_api.fake.posts == []

    # ...and no tool ran at all, which the audit trail is the authority on.
    events = client.get("/audit/events?limit=100").json()

    assert [event for event in events if event["event_type"] == "TOOL_EXECUTED"] == []
    assert [event for event in events if event["event_type"] == "TOOL_REQUESTED"] == []


def test_identical_text_does_not_make_a_stale_draft_current(inbox_api):
    """The guard is tied to the fingerprint, never to the wording.

    A draft written before the guest's latest message is stale even if the words
    it carries happen to be exactly the words a fresh draft would carry. The
    same text submitted with the refreshed fingerprint is accepted, so it is
    demonstrably the fingerprint -- not the text -- that decided.
    """
    client = inbox_api.client("ADMIN")

    stale_fingerprint = current_fingerprint(client)

    guest_writes_again(inbox_api)

    refused = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(stale_fingerprint),
    )

    assert refused.status_code == 409, refused.text

    accepted = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(current_fingerprint(client)),
    )

    assert accepted.status_code == 200, accepted.text


def test_a_refreshed_fingerprint_is_accepted_after_the_conversation_moves_on(
    inbox_api,
):
    """Regenerating clears the block, because it re-reads the conversation.

    Regenerating a draft ends with the console reloading the conversation --
    that reload is what hands it the new fingerprint. Driving the model is not
    needed to prove the guard reopens; holding the *current* fingerprint is the
    whole of the condition.
    """
    client = inbox_api.client("ADMIN")

    stale_fingerprint = current_fingerprint(client)

    guest_writes_again(inbox_api)

    assert (
        client.post(
            f"/inbox/{REF}/reply",
            json=reply_payload(stale_fingerprint),
        ).status_code
        == 409
    )

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(current_fingerprint(client), body="Sure -- bags are fine."),
    )

    assert response.status_code == 200, response.text
    assert response.json()["approval_required"]["arguments"]["message"] == (
        "Sure -- bags are fine."
    )


def test_an_edited_but_current_draft_is_accepted(inbox_api):
    """Editing does not make a draft stale; only the conversation moving does."""
    client = inbox_api.client("ADMIN")

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(
            current_fingerprint(client),
            subject="Re: parking",
            body="There is one off-street space, reserved for you.",
        ),
    )

    assert response.status_code == 200, response.text

    approval = response.json()["approval_required"]

    assert approval["risk"] == "DANGEROUS"
    assert approval["arguments"]["message"] == (
        "There is one off-street space, reserved for you."
    )
    assert inbox_api.fake.posts == []


@pytest.mark.parametrize(
    "payload",
    [
        reply_payload(None),
        reply_payload(""),
        reply_payload("   "),
    ],
    ids=["missing", "blank", "whitespace"],
)
def test_a_submission_without_a_fingerprint_is_refused(inbox_api, payload):
    """Arbitrary text must still carry a current fingerprint.

    Rejected at the schema boundary rather than by the comparison, so a client
    that never looked at the conversation is refused whatever the provider is
    doing -- including during the fail-open window below.
    """
    response = inbox_api.client("ADMIN").post(f"/inbox/{REF}/reply", json=payload)

    assert response.status_code == 422, response.text
    assert inbox_api.module.approval_store.list_approvals() == []
    assert inbox_api.fake.posts == []


def test_an_unreadable_conversation_allows_the_submission_and_still_gates_it(
    inbox_api,
):
    """The documented fail-open: unknown currency is not treated as stale.

    A provider hiccup must not make every prepared reply unsendable. What the
    submission buys is a PENDING DANGEROUS approval, not a send -- the human
    gate is untouched, so the fail-open is bounded by it.
    """
    client = inbox_api.client("ADMIN")

    fingerprint = current_fingerprint(client)

    # Every subsequent thread read fails, so the live fingerprint is unknowable.
    inbox_api.fake.thread_failures[THREAD_A] = 503

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(fingerprint),
    )

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["status"] == "WAITING_FOR_APPROVAL"
    assert payload["approval_required"]["risk"] == "DANGEROUS"

    approvals = inbox_api.module.approval_store.list_approvals()

    assert [approval["status"] for approval in approvals] == ["PENDING"]

    # Nothing was sent by allowing it.
    assert inbox_api.fake.posts == []


def test_an_unknown_conversation_ref_is_refused_with_404(inbox_api):
    """A ref that matches no booking can never reach `send_guest_reply`.

    `current_fingerprint_for` folds "does not exist" into the same `None` as
    "could not be read" -- correct for the draft-summary routes, which should
    render an unknown ref as if the draft were current rather than error. The
    reply route needs the opposite: parking a DANGEROUS approval for a ref
    that can never execute wastes an approver's authority on nothing.
    """
    response = inbox_api.client("ADMIN").post(
        f"/inbox/{UNKNOWN_REF}/reply",
        json=reply_payload("does-not-matter"),
    )

    assert response.status_code == 404, response.text
    assert "Unknown conversation" in response.json()["detail"]


def test_an_unknown_conversation_ref_creates_no_approval(inbox_api):
    inbox_api.client("ADMIN").post(
        f"/inbox/{UNKNOWN_REF}/reply",
        json=reply_payload("does-not-matter"),
    )

    assert inbox_api.module.approval_store.list_approvals() == []


def test_an_unknown_conversation_ref_sends_nothing_and_executes_no_tool(inbox_api):
    client = inbox_api.client("ADMIN")

    client.post(
        f"/inbox/{UNKNOWN_REF}/reply",
        json=reply_payload("does-not-matter"),
    )

    assert inbox_api.fake.posts == []

    events = client.get("/audit/events?limit=100").json()

    assert [e for e in events if e["event_type"] == "TOOL_EXECUTED"] == []
    assert [e for e in events if e["event_type"] == "TOOL_REQUESTED"] == []


def test_a_provider_outage_on_reply_is_not_treated_as_unknown(inbox_api):
    """Temporarily unreadable is not the same as nonexistent.

    `LodgifyUnavailable` must stay on the documented fail-open path -- see
    `test_an_unreadable_conversation_allows_the_submission_and_still_gates_it`
    for the full behaviour that must be preserved. This asserts the one
    property that separates it from
    `test_an_unknown_conversation_ref_is_refused_with_404`: an outage is never
    confused with "does not exist".
    """
    client = inbox_api.client("ADMIN")

    fingerprint = current_fingerprint(client)

    inbox_api.fake.thread_failures[THREAD_A] = 503

    response = client.post(
        f"/inbox/{REF}/reply",
        json=reply_payload(fingerprint),
    )

    assert response.status_code != 404, response.text


def test_submission_never_executes_the_send_tool(inbox_api):
    """The pre-existing protection, restated against the guard.

    Adding a rejection path must not have turned the accepted path into an
    execution: a successful submission still runs no tool and sends nothing.
    """
    client = inbox_api.client("ADMIN")

    client.post(f"/inbox/{REF}/reply", json=reply_payload(current_fingerprint(client)))

    events = client.get("/audit/events?limit=100").json()

    types = [event["event_type"] for event in events]

    assert "APPROVAL_REQUIRED" in types
    assert "TOOL_EXECUTED" not in types
    assert inbox_api.fake.posts == []
    assert inbox_api.module.tool_registry.get("send_guest_reply").risk.value == (
        "DANGEROUS"
    )


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


# -- the conversation activity index ----------------------------------------


def test_inbox_rows_expose_the_preview_flag(inbox_api):
    rows = inbox_api.client("ADMIN").get("/inbox").json()["conversations"]

    assert rows
    assert all("preview_unavailable" in row for row in rows)


def test_a_webhook_known_conversation_reaches_the_api(inbox_api):
    """End to end: what a webhook remembered is listed by the route.

    The module-level `activity_store` is bound to `inbox_api.module.database`
    -- the isolated, per-test database the `api` fixture points
    AGENTOPS_DATABASE_URL at -- so writing through it here is writing to
    exactly what the route reads from. Using `inbox_api.module.activity_store`
    directly keeps that binding explicit instead of constructing a second
    store by hand.
    """
    inbox_api.module.activity_store.upsert(
        conversation_ref="PH-HISTORIC1",
        conversation_fingerprint="fp-1",
        status="needs_attention",
        last_message_at="2026-09-03T12:06:33",
        last_message_sender="Renter",
        message_count=2,
        property_slug="renovated-2nd-floor-home",
        source="BookingCom",
        booking_status="Booked",
    )

    rows = inbox_api.client("ADMIN").get("/inbox").json()["conversations"]

    assert rows[0]["conversation_ref"] == "PH-HISTORIC1"
