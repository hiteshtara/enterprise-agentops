"""The governed enquiry send: generate, edit, approve, send exactly once.

The counterpart to tests/test_enquiry_replies.py, which covers drafting and
proves nothing on that path can write. This file covers the one path that can,
and it is mostly about what the path refuses to do:

  * nothing is sent without a recorded human approval;
  * the operator's edited text is what reaches the approval record, and what
    reaches Lodgify, byte for byte;
  * exactly one POST is issued, to the documented **enquiry** endpoint, and the
    booking endpoint is never called for an enquiry under any outcome;
  * `UNKNOWN_SEND_STATE` is neither a failure nor a retry -- it asks for a
    person, in words;
  * `send_enquiry_reply` is registered, risk-tiered and approval-gated, and the
    model is never told it exists.

Every payload is invented. No test opens a socket, calls a model, touches the
development database, or issues a real send.
"""

import ast
import json
import pathlib

import httpx
import pytest

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.connectors.lodgify.enquiry_tools import (
    SEND_ENQUIRY_REPLY_SCHEMA,
    LodgifyEnquiryTools,
)
from app.connectors.lodgify.messaging_models import SendStatus
from app.connectors.lodgify.refs import conversation_ref_for, enquiry_ref_for
from app.enquiry_replies import EnquiryReplyService
from app.observability_store import ModelExecutionStore, ToolExecutionStore
from app.run_store import RunStore
from app.tool_registry import ApprovalRequired, ToolRisk
from app.tool_setup import build_tool_registry, send_enquiry_reply_tool
from tests.fakes import ScriptedModelProvider, final_response
from tests.lodgify_fakes import (
    THREAD_A,
    THREAD_B,
    FakeLodgify,
    booking_row,
    enquiry_row,
    message,
    thread,
)

TOOL = "send_enquiry_reply"

ENQUIRY_ID = 900101

BOOKING_ID = 800201

REF = enquiry_ref_for(ENQUIRY_ID)

UNKNOWN_REF = "EQ-ZZZZZZZZ"

SUBJECT = "Re: your enquiry"

DRAFT_TEXT = (
    "Thank you for getting in touch. I will confirm those dates and the total "
    "for you and come straight back."
)

# What an operator typed over the draft. Deliberately multi-line and
# punctuated, so "byte for byte" is a claim with something to catch.
EDITED = (
    "Hello, and thanks for your enquiry!\n\n"
    "I'll check those dates and come back to you today with the total -- "
    "including the cleaning fee, so there are no surprises.\n\n"
    "Best wishes,\nPriyanka Homes"
)

ASK = message(
    "m-enq-1",
    "Renter",
    "Hi, is the flat free for four nights in December, and is parking included?",
    "2026-09-01T09:05:00",
    subject="Booking enquiry",
    message_status=None,
    route=None,
)

# A payload shape the client cannot read as a thread, which is how a provider
# read failure is reproduced without a socket.
UNREADABLE: list = []


def open_list() -> list[dict]:
    """One enquiry and one booking, as upstream mixes them in one list."""
    return [
        enquiry_row(ENQUIRY_ID, THREAD_A, created_at="2026-09-01T09:00:00"),
        booking_row(BOOKING_ID, THREAD_B, created_at="2026-09-01T08:00:00"),
    ]


def sent_row(subject: str = SUBJECT, body: str = EDITED) -> dict:
    """The row the provider grows when our send lands."""
    return message(
        "m-enq-sent",
        "Owner",
        body,
        "2026-09-01T10:00:00",
        subject=subject,
        message_status="Sent",
        route=None,
    )


def send_fake(
    after: list[dict] | None = None,
    snapshot: object = None,
    verification: object = None,
    **kwargs,
) -> FakeLodgify:
    """A provider scripted for one send: snapshot read, POST, verification read.

    `after` is the thread as it stands once our message has landed. `snapshot`
    and `verification` override either read outright, which is how an
    unreadable thread is reproduced.
    """
    landed = thread(THREAD_A, after if after is not None else [ASK, sent_row()])

    return FakeLodgify(
        reservations=open_list(),
        thread_sequence={
            THREAD_A: [
                thread(THREAD_A, [ASK]) if snapshot is None else snapshot,
                landed if verification is None else verification,
            ]
        },
        **kwargs,
    )


def send(fake: FakeLodgify, subject: str = SUBJECT, body: str = EDITED) -> dict:
    return fake.enquiry_sender().send_reply(
        enquiry_ref=REF,
        subject=subject,
        message=body,
    )


# -- the endpoint ----------------------------------------------------------


def test_a_confirmed_send_posts_once_to_the_enquiry_endpoint():
    fake = send_fake()

    outcome = send(fake)

    assert outcome["status"] == SendStatus.CONFIRMED_SENT.value
    assert outcome["enquiry_ref"] == REF

    assert len(fake.posts) == 1
    assert fake.posts[0].url.path == f"/v1/reservation/enquiry/{ENQUIRY_ID}/messages"


def test_the_booking_endpoint_is_never_called_for_an_enquiry():
    fake = send_fake()

    send(fake)

    assert fake.booking_posts == []
    assert len(fake.enquiry_posts) == 1
    assert str(BOOKING_ID) not in str(fake.posts[0].url)


@pytest.mark.parametrize(
    "failure",
    [
        {"post_status": 400},
        {"post_status": 500},
        {"post_raises": httpx.ReadTimeout("scripted")},
        {"post_raises": httpx.ConnectError("scripted")},
    ],
)
def test_no_failure_mode_falls_back_to_the_booking_endpoint(failure):
    """There is no fallback between the two send endpoints, on any path."""
    fake = send_fake(**failure)

    send(fake)

    assert fake.booking_posts == []
    assert len(fake.posts) <= 1


def test_the_send_body_pins_type_and_notification():
    """Who it comes from and whether anyone is told are not caller decisions."""
    fake = send_fake()

    send(fake)

    body = json.loads(fake.posts[0].content)

    assert body == [
        {
            "subject": SUBJECT,
            "message": EDITED,
            "type": "Owner",
            "send_notification": True,
        }
    ]


def test_the_send_carries_the_api_key_and_no_session_credential():
    fake = send_fake()

    send(fake)

    for request in fake.requests:
        assert request.url.host == "api.lodgify.com"
        assert request.headers.get("X-ApiKey")
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers


def test_exactly_one_post_is_issued_and_nothing_else_is_written():
    """Snapshot, one POST, verification. No modification of any reservation."""
    fake = send_fake()

    send(fake)

    methods = {request.method for request in fake.requests}

    assert methods == {"GET", "POST"}
    assert len(fake.posts) == 1

    for request in fake.requests:
        if request.method == "GET":
            continue

        assert request.url.path.endswith("/messages")


# -- the approved text is the sent text ------------------------------------


def test_the_approved_text_is_transmitted_byte_for_byte():
    fake = send_fake(after=[ASK, sent_row(body=EDITED)])

    outcome = send(fake)

    assert json.loads(fake.posts[0].content)[0]["message"] == EDITED
    assert outcome["status"] == SendStatus.CONFIRMED_SENT.value


@pytest.mark.parametrize(
    "bad",
    [
        "<b>Hello</b>",
        "Hello\x00there",
    ],
)
def test_invalid_text_is_rejected_and_never_rewritten(bad):
    fake = send_fake()

    with pytest.raises(ValueError):
        send(fake, body=bad)

    assert fake.posts == []


def test_a_non_string_message_is_a_type_error_and_sends_nothing():
    fake = send_fake()

    with pytest.raises(TypeError):
        send(fake, body=None)

    assert fake.posts == []


# -- the three outcomes ----------------------------------------------------


def test_a_provider_rejection_is_confirmed_failed():
    fake = send_fake(post_status=400)

    outcome = send(fake)

    assert outcome["status"] == SendStatus.CONFIRMED_FAILED.value
    assert "Nothing was sent" in outcome["message"]
    assert outcome["messages"] == []


def test_an_unreadable_thread_refuses_before_sending():
    fake = send_fake(snapshot=UNREADABLE)

    outcome = send(fake)

    assert outcome["status"] == SendStatus.CONFIRMED_FAILED.value
    assert "nothing was sent" in outcome["message"]
    assert fake.posts == []


def test_a_server_error_is_unknown_and_asks_for_a_person():
    fake = send_fake(post_status=500)

    outcome = send(fake)

    assert outcome["status"] == SendStatus.UNKNOWN_SEND_STATE.value
    assert "Do not resend automatically" in outcome["message"]
    assert "Check the Lodgify thread" in outcome["message"]

    # One attempt, and no second one.
    assert len(fake.posts) == 1


def test_an_ambiguous_transport_failure_is_unknown_and_not_retried():
    fake = send_fake(post_raises=httpx.ReadTimeout("scripted"))

    outcome = send(fake)

    assert outcome["status"] == SendStatus.UNKNOWN_SEND_STATE.value
    assert len(fake.posts) == 1


def test_a_verification_read_failure_is_unknown_not_failure():
    """The message may well have gone. Reporting failure would be a lie."""
    fake = send_fake(verification=UNREADABLE)

    outcome = send(fake)

    assert outcome["status"] == SendStatus.UNKNOWN_SEND_STATE.value
    assert len(fake.posts) == 1


def test_a_thread_that_does_not_show_our_message_is_unknown():
    fake = send_fake(after=[ASK])

    outcome = send(fake)

    assert outcome["status"] == SendStatus.UNKNOWN_SEND_STATE.value


def test_a_connection_that_was_never_made_is_a_clean_failure():
    fake = send_fake(post_raises=httpx.ConnectError("scripted"))

    outcome = send(fake)

    assert outcome["status"] == SendStatus.CONFIRMED_FAILED.value


# -- what the outcome may carry --------------------------------------------


FORBIDDEN = (
    "Fixture Enquirer",
    "fixture.enquirer@example.invalid",
    "+15550000001",
    THREAD_A,
    str(ENQUIRY_ID),
    str(BOOKING_ID),
    "guest_name",
    "source_text",
    "987.65",
)


def test_the_outcome_carries_no_pii_and_no_provider_identifier():
    dumped = json.dumps(send(send_fake()))

    for secret in FORBIDDEN:
        assert secret not in dumped


def test_an_unknown_ref_raises_before_any_post():
    fake = send_fake()

    with pytest.raises(ValueError):
        fake.enquiry_sender().send_reply(
            enquiry_ref=UNKNOWN_REF,
            subject=SUBJECT,
            message=EDITED,
        )

    assert fake.posts == []


def test_a_conversation_ref_cannot_address_an_enquiry():
    fake = send_fake()

    with pytest.raises(ValueError):
        fake.enquiry_sender().send_reply(
            enquiry_ref=conversation_ref_for(ENQUIRY_ID),
            subject=SUBJECT,
            message=EDITED,
        )

    assert fake.posts == []


# -- governance ------------------------------------------------------------


def enquiry_registry(migration_store, fake: FakeLodgify):
    return build_tool_registry(
        migration_store=migration_store,
        lodgify_enquiries=LodgifyEnquiryTools(fake.enquiry_sender()),
    )


def test_the_send_tool_is_dangerous_and_hidden_from_the_model(migration_store):
    registry = enquiry_registry(migration_store, send_fake())

    assert registry.get(TOOL).risk is ToolRisk.DANGEROUS
    assert registry.get(TOOL).model_callable is False

    advertised = {definition.name for definition in registry.definitions()}

    assert TOOL not in advertised

    # Not merely this tool: the model is told about no enquiry capability at
    # all, because a tool that let it name an enquiry would let it name any.
    assert not any("enquiry" in name for name in advertised)


def test_the_console_still_sees_the_tool_and_its_risk(migration_store):
    registry = enquiry_registry(migration_store, send_fake())

    described = {tool["name"]: tool for tool in registry.describe()}

    assert TOOL in described
    assert described[TOOL]["risk"] == ToolRisk.DANGEROUS.value
    assert described[TOOL]["parameters"]["additionalProperties"] is False


def test_the_hidden_tool_is_still_approval_gated(migration_store):
    fake = send_fake()
    registry = enquiry_registry(migration_store, fake)

    with pytest.raises(ApprovalRequired):
        registry.execute(
            TOOL,
            {"enquiry_ref": REF, "subject": SUBJECT, "message": EDITED},
        )

    # Nothing left the process.
    assert fake.requests == []


def test_an_approved_call_executes(migration_store):
    fake = send_fake()
    registry = enquiry_registry(migration_store, fake)

    outcome = registry.execute(
        TOOL,
        {"enquiry_ref": REF, "subject": SUBJECT, "message": EDITED},
        approved=True,
    )

    assert outcome["status"] == SendStatus.CONFIRMED_SENT.value
    assert len(fake.posts) == 1


def test_the_registry_is_unchanged_without_the_enquiry_connector(migration_store):
    registry = build_tool_registry(migration_store=migration_store)

    assert registry.get(TOOL) is None
    assert TOOL not in {tool["name"] for tool in registry.describe()}


def test_the_schema_exposes_exactly_three_fields():
    assert set(SEND_ENQUIRY_REPLY_SCHEMA["properties"]) == {
        "enquiry_ref",
        "subject",
        "message",
    }
    assert SEND_ENQUIRY_REPLY_SCHEMA["additionalProperties"] is False


@pytest.mark.parametrize(
    "forbidden",
    ["type", "send_notification", "enquiry_id", "booking_id", "thread_uid", "route"],
)
def test_the_schema_hides_every_provider_controlled_field(forbidden):
    assert forbidden not in SEND_ENQUIRY_REPLY_SCHEMA["properties"]


def test_no_caller_can_supply_type_or_send_notification(migration_store):
    fake = send_fake()
    registry = enquiry_registry(migration_store, fake)

    with pytest.raises(TypeError):
        registry.execute(
            TOOL,
            {
                "enquiry_ref": REF,
                "subject": SUBJECT,
                "message": EDITED,
                "type": "Renter",
                "send_notification": False,
            },
            approved=True,
        )

    assert fake.posts == []


# -- source-level guarantees ----------------------------------------------


ENQUIRY_PATH_SOURCES = (
    "app/connectors/lodgify/enquiries.py",
    "app/connectors/lodgify/enquiry_tools.py",
    "app/connectors/lodgify/messaging_client.py",
    "app/enquiry_replies.py",
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def source(relative: str) -> str:
    return (REPO_ROOT / relative).read_text()


def code_only(relative: str) -> str:
    """The module's executable text: comments and docstrings removed.

    These modules document at length what they refuse to talk to, so a plain
    substring scan matches the prose rather than the code. String literals are
    kept -- a forbidden host would be one -- and only the narration is dropped.
    """
    tree = ast.parse(source(relative))

    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ) and ast.get_docstring(node):
            node.body = node.body[1:]

    return ast.unparse(tree).lower()


@pytest.mark.parametrize("relative", ENQUIRY_PATH_SOURCES)
def test_no_private_host_or_session_credential_appears_in_the_enquiry_path(relative):
    text = code_only(relative)

    for forbidden in (
        "app.lodgify.com",
        "ai-assistant",
        "cookie",
        "bearer",
        "session_token",
        "csrf",
    ):
        assert forbidden not in text, forbidden


@pytest.mark.parametrize("relative", ENQUIRY_PATH_SOURCES)
def test_nothing_on_the_enquiry_path_retries_a_send(relative):
    """The ban is on a mechanism, not on the word -- the prose says "no retry"."""
    text = code_only(relative)

    for forbidden in ("retry", "retries", "backoff", "tenacity"):
        assert forbidden not in text, forbidden


def test_only_the_enquiry_sender_can_reach_the_enquiry_post():
    """One caller, one call site. There is no second path to the provider."""
    callers = [
        path
        for path in (REPO_ROOT / "app").rglob("*.py")
        if "post_enquiry_message(" in path.read_text()
    ]

    assert {path.name for path in callers} == {"messaging_client.py", "enquiries.py"}


def test_no_module_sends_on_its_own():
    """The tool is invoked from the route only, never from a background pass."""
    invokers = [
        path
        for path in (REPO_ROOT / "app").rglob("*.py")
        if f'"{TOOL}"' in path.read_text()
    ]

    assert {path.name for path in invokers} == {"tool_setup.py", "main.py"}


# -- the HTTP surface ------------------------------------------------------


def build_agent(database, registry, model) -> AgentService:
    return AgentService(
        model=model,
        tool_registry=registry,
        approval_store=ApprovalStore(database=database),
        audit_store=AuditStore(database=database),
        run_store=RunStore(database=database),
        model_executions=ModelExecutionStore(database=database),
        tool_executions=ToolExecutionStore(database=database),
    )


@pytest.fixture
def send_api(api):
    """The reloaded app with a scripted Lodgify and a scripted model behind it.

    The connector is unconfigured in tests, so the module builds none of these
    objects. They are installed afterwards, including the registration of the
    send tool into the module's own registry -- which is what the agent and the
    approval route both read.
    """
    module = api.module

    fake = send_fake()
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)] * 4)

    module.lodgify_enquiries = fake.enquiries()
    module.enquiry_sender = fake.enquiry_sender()
    module.enquiry_replies = EnquiryReplyService(
        enquiries=module.lodgify_enquiries,
        agent=build_agent(module.database, module.tool_registry, model),
        knowledge=module.knowledge_store,
    )
    module.tool_registry.register(
        send_enquiry_reply_tool(LodgifyEnquiryTools(module.enquiry_sender))
    )

    api.fake = fake
    api.model = model

    return api


def submit(client, subject: str = SUBJECT, body: str = EDITED):
    return client.post(
        f"/enquiries/{REF}/reply",
        json={"subject": subject, "message": body},
    )


def test_submitting_creates_a_pending_approval_and_sends_nothing(send_api):
    response = submit(send_api.client())

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "WAITING_FOR_APPROVAL"
    assert body["approval_required"]["tool"] == TOOL
    assert body["approval_required"]["risk"] == "DANGEROUS"

    # The whole point: no provider write, and no thread read either.
    assert send_api.fake.posts == []
    assert send_api.fake.requests == []


def test_the_edited_text_reaches_the_approval_record_byte_for_byte(send_api):
    client = send_api.client()

    draft = client.post(f"/enquiries/{REF}/reply-draft").json()

    assert draft["message"] == DRAFT_TEXT

    # The operator rewrites the draft before submitting it.
    approval = submit(client, body=EDITED).json()["approval_required"]

    assert approval["arguments"]["message"] == EDITED
    assert approval["arguments"]["subject"] == SUBJECT
    assert approval["arguments"]["enquiry_ref"] == REF

    stored = send_api.module.approval_store.get(approval["approval_id"])

    assert stored.arguments["message"] == EDITED
    assert stored.status == "PENDING"


def test_approval_sends_once_and_reports_confirmed_sent(send_api):
    client = send_api.client()

    approval = submit(client).json()["approval_required"]

    assert send_api.fake.posts == []

    response = client.post(
        f"/agent/approvals/{approval['approval_id']}",
        json={"approved": True},
    )

    assert response.status_code == 200

    result = response.json()["result"]

    assert result["status"] == SendStatus.CONFIRMED_SENT.value
    assert result["enquiry_ref"] == REF

    assert len(send_api.fake.posts) == 1
    assert send_api.fake.booking_posts == []
    assert json.loads(send_api.fake.posts[0].content)[0]["message"] == EDITED


def test_rejecting_sends_nothing(send_api):
    client = send_api.client()

    approval = submit(client).json()["approval_required"]

    response = client.post(
        f"/agent/approvals/{approval['approval_id']}",
        json={"approved": False},
    )

    assert response.status_code == 200
    assert response.json()["run_status"] == "CANCELLED"
    assert send_api.fake.posts == []


def test_a_confirmed_failure_is_reported_and_marks_nothing_sent(api):
    fake = send_fake(post_status=400)

    client = install(api, fake).client()

    approval = submit(client).json()["approval_required"]

    result = client.post(
        f"/agent/approvals/{approval['approval_id']}",
        json={"approved": True},
    ).json()["result"]

    assert result["status"] == SendStatus.CONFIRMED_FAILED.value
    assert result["messages"] == []


def test_an_unknown_send_state_is_not_retried_and_cannot_be_resolved_twice(api):
    fake = send_fake(post_status=500)

    client = install(api, fake).client()

    approval = submit(client).json()["approval_required"]

    result = client.post(
        f"/agent/approvals/{approval['approval_id']}",
        json={"approved": True},
    ).json()["result"]

    assert result["status"] == SendStatus.UNKNOWN_SEND_STATE.value
    assert "Do not resend automatically" in result["message"]

    # One attempt, and the approval cannot be replayed into a second one.
    assert len(fake.posts) == 1

    again = client.post(
        f"/agent/approvals/{approval['approval_id']}",
        json={"approved": True},
    )

    assert again.status_code >= 400
    assert len(fake.posts) == 1


def install(api, fake: FakeLodgify):
    """Wire one scripted provider into the reloaded app."""
    module = api.module

    module.lodgify_enquiries = fake.enquiries()
    module.enquiry_sender = fake.enquiry_sender()
    module.tool_registry.register(
        send_enquiry_reply_tool(LodgifyEnquiryTools(module.enquiry_sender))
    )

    return api


def test_no_reservation_is_ever_modified_by_the_whole_flow(send_api):
    client = send_api.client()

    client.get("/enquiries")
    client.post(f"/enquiries/{REF}/reply-draft")

    approval = submit(client).json()["approval_required"]

    client.post(
        f"/agent/approvals/{approval['approval_id']}",
        json={"approved": True},
    )

    for request in send_api.fake.requests:
        assert request.method in {"GET", "POST"}

        if request.method == "POST":
            assert request.url.path == (
                f"/v1/reservation/enquiry/{ENQUIRY_ID}/messages"
            )


def test_the_submit_route_refuses_a_malformed_ref(send_api):
    response = send_api.client().post(
        "/enquiries/not-a-ref/reply",
        json={"subject": SUBJECT, "message": EDITED},
    )

    assert response.status_code == 404
    assert send_api.fake.requests == []


def test_the_submit_route_refuses_empty_text(send_api):
    response = submit(send_api.client(), body="")

    assert response.status_code == 422
    assert send_api.fake.requests == []


def test_the_submit_route_requires_a_credential(send_api):
    response = send_api.anonymous().post(
        f"/enquiries/{REF}/reply",
        json={"subject": SUBJECT, "message": EDITED},
    )

    assert response.status_code == 401


def test_a_viewer_cannot_submit_a_reply(send_api):
    assert submit(send_api.client("VIEWER")).status_code == 403


def test_an_operator_can_submit_but_not_approve(send_api):
    client = send_api.client("OPERATOR")

    approval = submit(client).json()["approval_required"]

    response = client.post(
        f"/agent/approvals/{approval['approval_id']}",
        json={"approved": True},
    )

    assert response.status_code == 403
    assert send_api.fake.posts == []


def test_the_submit_route_is_unavailable_without_the_connector(api):
    api.module.lodgify_enquiries = None

    assert submit(api.client()).status_code == 503
