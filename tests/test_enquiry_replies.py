"""The on-demand enquiry reply generator.

A deliberately narrow surface: two read routes, one model call per press, and
nothing written anywhere. The tests here are mostly about what does *not*
happen -- no booking row treated as an enquiry, no ignored provider filter
relied on, no provider identifier or guest detail crossing the API boundary, no
row stored, and no POST issued to Lodgify from any drafting path.

The governed send that an operator can submit *after* reading a draft lives in
tests/test_enquiry_send.py. Nothing in this file can reach it: the drafting
service is given the reader, which has no write method.

Every payload is invented. No test opens a socket, calls a model, or touches
the development database.
"""

import json
import pathlib

import pytest
from sqlalchemy import func, select

from app.agent import AgentService
from app.approval_store import ApprovalStore
from app.audit_store import AuditStore
from app.connectors.lodgify.enquiries import PAGE_SIZE
from app.connectors.lodgify.refs import (
    conversation_ref_for,
    enquiry_ref_for,
    is_well_formed,
    is_well_formed_enquiry_ref,
)
from app.db_models import ConversationActivityRecord, ConversationDraftRecord
from app.enquiry_replies import (
    DECLINED_DETAIL,
    DRAFTED_DETAIL,
    MODEL_FAILED_DETAIL,
    NOTHING_TO_SAY_DETAIL,
    OPEN_THREAD_INSTRUCTION,
    SETTLED_THREAD_INSTRUCTION,
    UNREADABLE_DETAIL,
    EnquiryReplyService,
)
from app.hospitality import (
    BUSINESS_SENSITIVE_ROUTING,
    NO_REPLY_GUIDANCE,
    analyse_conversation,
    enquiry_reply_guidance,
    reply_guidance,
)
from app.knowledge import KnowledgeStore
from app.knowledge_topics import GUEST_FACING
from app.observability_store import ModelExecutionStore, ToolExecutionStore
from app.run_store import RunStore
from tests.fakes import ScriptedModelProvider, final_response
from tests.lodgify_fakes import (
    THREAD_A,
    THREAD_B,
    FakeLodgify,
    booking_row,
    closed_period_row,
    enquiry_row,
    message,
    thread,
)

ENQUIRY_ID = 900101

OTHER_ENQUIRY_ID = 900102

REF = enquiry_ref_for(ENQUIRY_ID)

# Well-formed but naming no enquiry in the fixture list -- what `resolve`
# refuses, and the route turns into a 404.
UNKNOWN_REF = "EQ-ZZZZZZZZ"

DRAFT_TEXT = (
    "Thank you for your enquiry. Those dates are available -- shall I hold them?"
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


def open_list() -> list[dict]:
    """One enquiry, one booking and one calendar block, as upstream mixes them."""
    return [
        enquiry_row(ENQUIRY_ID, THREAD_A, created_at="2026-09-01T09:00:00"),
        booking_row(800201, THREAD_B, created_at="2026-09-01T08:00:00"),
        closed_period_row(700301, created_at="2026-09-01T07:00:00"),
    ]


def fake_provider(**overrides) -> FakeLodgify:
    return FakeLodgify(
        reservations=overrides.pop("reservations", open_list()),
        threads=overrides.pop("threads", {THREAD_A: thread(THREAD_A, [ASK])}),
        **overrides,
    )


# -- listing ---------------------------------------------------------------


def test_list_returns_only_enquiry_rows():
    """Bookings and calendar blocks are excluded, in our code."""
    rows = fake_provider().enquiries().list_open()

    assert [row["enquiry_ref"] for row in rows] == [REF]


def test_closed_periods_never_appear_even_when_they_carry_a_thread():
    """A calendar block is not a conversation, whatever fields it happens to have."""
    reservations = [
        closed_period_row(700301),
        # The pathological case: a ClosedPeriod row that does have a thread, so
        # only the `type` test can exclude it.
        {**closed_period_row(700302), "thread_uid": THREAD_B},
    ]

    assert fake_provider(reservations=reservations).enquiries().list_open() == []


def test_paging_sends_offset_and_limit_and_never_page_or_size():
    """The provider ignores `page`/`size`; sending them would loop on page one."""
    # One enquiry on the second page, so the walk has to reach it to pass.
    reservations = [
        booking_row(800000 + index, THREAD_B) for index in range(PAGE_SIZE)
    ] + [enquiry_row(ENQUIRY_ID, THREAD_A)]

    fake = fake_provider(reservations=reservations)

    rows = fake.enquiries().list_open()

    assert [row["enquiry_ref"] for row in rows] == [REF]

    offsets = []

    for request in fake.reservation_reads:
        params = request.url.params

        assert "page" not in params
        assert "size" not in params
        assert params["limit"] == str(PAGE_SIZE)
        assert params["status"] == "Open"

        offsets.append(params["offset"])

    assert offsets == ["0", str(PAGE_SIZE)]


def test_the_ignored_provider_type_filter_is_not_relied_on():
    """No `type` parameter is sent, because the provider ignores it.

    The fixture answers with Booking and ClosedPeriod rows regardless of any
    filter, exactly as the live account does, so a connector that trusted the
    parameter would return them.
    """
    fake = fake_provider()

    rows = fake.enquiries().list_open()

    assert all("type" not in request.url.params for request in fake.reservation_reads)
    assert len(rows) == 1


def test_paging_stops_at_a_short_page():
    """Three rows is one page, so exactly one request is made."""
    fake = fake_provider()

    fake.enquiries().list_open()

    assert len(fake.reservation_reads) == 1


def test_limit_bounds_the_response():
    reservations = [
        enquiry_row(ENQUIRY_ID + index, THREAD_A, created_at=f"2026-09-0{index + 1}")
        for index in range(3)
    ]

    rows = fake_provider(reservations=reservations).enquiries().list_open(limit=2)

    assert len(rows) == 2


def test_a_bad_limit_raises_rather_than_being_coerced():
    enquiries = fake_provider().enquiries()

    with pytest.raises(ValueError):
        enquiries.list_open(limit=0)

    with pytest.raises(TypeError):
        enquiries.list_open(limit="5")


# -- the reference ---------------------------------------------------------


def test_enquiry_ref_is_opaque_and_stable():
    ref = enquiry_ref_for(ENQUIRY_ID)

    assert ref == enquiry_ref_for(ENQUIRY_ID)
    assert str(ENQUIRY_ID) not in ref
    assert is_well_formed_enquiry_ref(ref)


def test_enquiry_ref_cannot_collide_with_a_conversation_ref():
    """Different prefix and a different digest body, for the same id."""
    booking = conversation_ref_for(ENQUIRY_ID)
    enquiry = enquiry_ref_for(ENQUIRY_ID)

    assert enquiry != booking
    assert enquiry.removeprefix("EQ-") != booking.removeprefix("PH-")

    # Neither shape check accepts the other's reference, so a ref cannot be
    # replayed from one surface onto the other.
    assert not is_well_formed(enquiry)
    assert not is_well_formed_enquiry_ref(booking)


def test_a_fabricated_ref_resolves_to_nothing():
    with pytest.raises(ValueError):
        fake_provider().enquiries().resolve(UNKNOWN_REF)


def test_a_conversation_ref_cannot_address_an_enquiry():
    with pytest.raises(ValueError):
        fake_provider().enquiries().resolve(conversation_ref_for(ENQUIRY_ID))


# -- sanitization ----------------------------------------------------------

# Everything the fixture rows carry that must never travel.
FORBIDDEN = (
    "Fixture Enquirer",
    "fixture.enquirer@example.invalid",
    "+15550000001",
    str(ENQUIRY_ID),
    THREAD_A,
    "555001",
    "listingId",
    "9999999999",
    "987.65",
    "Upstream Property Title",
)


def test_the_listed_row_carries_no_pii_and_no_provider_identifier():
    dumped = json.dumps(fake_provider().enquiries().list_open())

    for secret in FORBIDDEN:
        assert secret not in dumped

    assert set(json.loads(dumped)[0]) == {
        "enquiry_ref",
        "property_slug",
        "property_name",
        "source",
        "arrival",
        "departure",
        "is_replied",
    }


# -- drafting --------------------------------------------------------------


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


def build_service(fake, database, registry, model) -> EnquiryReplyService:
    return EnquiryReplyService(
        enquiries=fake.enquiries(),
        agent=build_agent(database, registry, model),
    )


def test_drafting_reads_the_thread_and_returns_text(database, registry):
    fake = fake_provider()
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)])

    draft = build_service(fake, database, registry, model).draft(REF)

    assert draft.message == DRAFT_TEXT
    assert draft.subject == "Re: your enquiry"
    assert draft.detail == DRAFTED_DETAIL
    assert draft.enquiry_ref == REF

    # The thread really was read, through the documented messaging endpoint.
    assert [request.url.path for request in fake.thread_reads] == [
        f"/v2/messaging/{THREAD_A}"
    ]


def test_the_prompt_carries_the_thread_and_the_house_rules(database, registry):
    """One model call, and it arrives with everything it needs.

    No tool is offered a way to name an enquiry, so the application has to put
    the thread in front of the model itself.
    """
    fake = fake_provider()
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)])

    build_service(fake, database, registry, model).draft(REF)

    assert model.call_count == 1

    prompt = model.conversations[0][0].content

    assert "is parking included" in prompt
    assert "never_invent" in prompt
    assert "authority_order" in prompt
    # The provider identifiers are not in the prompt either.
    assert THREAD_A not in prompt
    assert str(ENQUIRY_ID) not in prompt


def test_an_unreadable_thread_returns_a_message_not_a_fabrication(database, registry):
    fake = fake_provider(thread_failures={THREAD_A: 500})
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)])

    draft = build_service(fake, database, registry, model).draft(REF)

    assert draft.message is None
    assert draft.subject is None
    assert draft.detail == UNREADABLE_DETAIL

    # No model call was spent on a thread nobody could read.
    assert model.call_count == 0


def test_an_empty_answer_never_becomes_a_reply(database, registry):
    fake = fake_provider()
    model = ScriptedModelProvider([final_response("   ")])

    draft = build_service(fake, database, registry, model).draft(REF)

    assert draft.message is None
    assert draft.detail == MODEL_FAILED_DETAIL


def test_an_unknown_enquiry_raises_rather_than_drafting(database, registry):
    fake = fake_provider()
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)])

    with pytest.raises(ValueError):
        build_service(fake, database, registry, model).draft(UNKNOWN_REF)


def test_drafting_issues_no_provider_write(database, registry):
    fake = fake_provider()
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)])

    build_service(fake, database, registry, model).draft(REF)

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)


def test_drafting_stores_nothing(database, registry):
    """No draft row, no activity row. The text exists in the response only."""
    fake = fake_provider()
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)])

    build_service(fake, database, registry, model).draft(REF)

    with database.session() as session:
        drafts = session.execute(
            select(func.count()).select_from(ConversationDraftRecord)
        ).scalar_one()
        activity = session.execute(
            select(func.count()).select_from(ConversationActivityRecord)
        ).scalar_one()

    assert drafts == 0
    assert activity == 0


# -- the HTTP surface ------------------------------------------------------


@pytest.fixture
def enquiry_api(api):
    """The reloaded app with a scripted Lodgify and a scripted model behind it.

    The connector is unconfigured in tests, so the module builds neither
    object. Both are installed afterwards; the routes read them from module
    scope at call time.
    """
    module = api.module

    fake = fake_provider()
    model = ScriptedModelProvider([final_response(DRAFT_TEXT)] * 4)

    module.lodgify_enquiries = fake.enquiries()
    module.enquiry_replies = EnquiryReplyService(
        enquiries=module.lodgify_enquiries,
        agent=build_agent(module.database, module.tool_registry, model),
        knowledge=module.knowledge_store,
    )

    api.fake = fake
    api.model = model

    return api


def test_list_route_returns_only_enquiries(enquiry_api):
    response = enquiry_api.client().get("/enquiries")

    assert response.status_code == 200

    body = response.json()

    assert body["count"] == 1
    assert body["enquiries"][0]["enquiry_ref"] == REF
    assert body["enquiries"][0]["is_replied"] is False


def test_list_route_leaks_nothing(enquiry_api):
    dumped = enquiry_api.client().get("/enquiries").text

    for secret in FORBIDDEN:
        assert secret not in dumped


def test_draft_route_returns_text_and_leaks_nothing(enquiry_api):
    response = enquiry_api.client().post(f"/enquiries/{REF}/reply-draft")

    assert response.status_code == 200

    body = response.json()

    assert body["message"] == DRAFT_TEXT
    assert body["enquiry_ref"] == REF

    for secret in FORBIDDEN:
        assert secret not in response.text


def test_draft_route_persists_nothing(enquiry_api):
    enquiry_api.client().post(f"/enquiries/{REF}/reply-draft")

    with enquiry_api.module.database.session() as session:
        drafts = session.execute(
            select(func.count()).select_from(ConversationDraftRecord)
        ).scalar_one()
        activity = session.execute(
            select(func.count()).select_from(ConversationActivityRecord)
        ).scalar_one()

    assert drafts == 0
    assert activity == 0


def test_neither_read_route_ever_posts_to_the_provider(enquiry_api):
    client = enquiry_api.client()

    client.get("/enquiries")
    client.post(f"/enquiries/{REF}/reply-draft")

    assert enquiry_api.fake.posts == []


def test_the_drafting_service_holds_no_object_that_can_send(enquiry_api):
    """Structural: drafting is given the reader, which has no write method.

    The enquiry surface does have a send now -- `send_enquiry_reply`, DANGEROUS
    and behind a human approval, exercised in tests/test_enquiry_send.py. What
    must stay true is that *drafting* cannot reach it: the service holds a
    `LodgifyEnquiries`, and there is no send method on that object to call.
    """
    enquiries = enquiry_api.module.enquiry_replies._enquiries

    assert not hasattr(enquiries, "send_reply")
    assert not hasattr(enquiries, "post_message")
    assert not hasattr(enquiries, "post_enquiry_message")


def test_the_enquiry_surface_has_exactly_one_write_route(enquiry_api):
    """Two reads and one submission, which parks rather than sends."""
    paths = {route.path for route in enquiry_api.module.app.routes}

    assert {path for path in paths if path.startswith("/enquiries")} == {
        "/enquiries",
        "/enquiries/{enquiry_ref}/reply-draft",
        "/enquiries/{enquiry_ref}/reply",
    }


def test_an_unknown_enquiry_is_a_404(enquiry_api):
    response = enquiry_api.client().post(f"/enquiries/{UNKNOWN_REF}/reply-draft")

    assert response.status_code == 404


def test_both_routes_require_a_credential(enquiry_api):
    anonymous = enquiry_api.anonymous()

    assert anonymous.get("/enquiries").status_code == 401
    assert anonymous.post(f"/enquiries/{REF}/reply-draft").status_code == 401


def test_the_routes_are_unavailable_without_the_connector(enquiry_api):
    enquiry_api.module.lodgify_enquiries = None

    client = enquiry_api.client()

    assert client.get("/enquiries").status_code == 503
    assert client.post(f"/enquiries/{REF}/reply-draft").status_code == 503


def test_a_provider_outage_is_a_502_without_the_providers_words(enquiry_api):
    enquiry_api.module.lodgify_enquiries = FakeLodgify(
        reservations_status=500
    ).enquiries()

    response = enquiry_api.client().get("/enquiries")

    assert response.status_code == 502
    assert "500" not in response.text


# -- the enquiry divergence ------------------------------------------------
#
# The owner-approved divergence from the booked-guest stance, and the reason
# for it. An enquiry is almost by definition business-sensitive -- a stranger
# asks whether the dates are free and what it costs -- so the booked-guest rule
# "business-sensitive and not in approved knowledge, hand it to a person"
# declined nearly every enquiry. It is correct on the path that can *send*. On
# this path nothing is sent, nothing is stored as a reply, and an operator
# reads every word, so the human is the gate and the model's job is to be
# useful and honest rather than silent.
#
# What did not move is `never_invent`. These tests assert the divergence is
# about what may be *withheld* and never about what may be asserted: the model
# is pushed to write a holding reply, and is given no new licence to state a
# price, an availability or an amenity it cannot establish.

ASKS_AVAILABILITY = message(
    "m-enq-avail",
    "Renter",
    "Hello! Are those four nights in December still available for two of us?",
    "2026-09-01T09:05:00",
    subject="Booking enquiry",
    message_status=None,
    route=None,
)

ASKS_PRICE = message(
    "m-enq-price",
    "Renter",
    "Hi there, how much is it for the week of the 14th, and is there a "
    "cleaning fee on top?",
    "2026-09-01T09:05:00",
    subject="Booking enquiry",
    message_status=None,
    route=None,
)

ASKS_AMENITY = message(
    "m-enq-amenity",
    "Renter",
    "Quick question before we book -- does the apartment have a dishwasher?",
    "2026-09-01T09:05:00",
    subject="Booking enquiry",
    message_status=None,
    route=None,
)

ASKS_PARKING = message(
    "m-enq-parking",
    "Renter",
    "We'd be driving down. Is there parking for one car?",
    "2026-09-01T09:05:00",
    subject="Booking enquiry",
    message_status=None,
    route=None,
)

# Invented, and deliberately of the shape the owner asked for: it answers the
# person without asserting the fact the answer would need.
HOLDING_TEXT = (
    "Thanks for checking. Let me confirm the exact rate for those dates and "
    "I'll get back to you."
)

# The closing thread: a question, our answer, and a bare acknowledgement. This
# is the shape the live investigation found behind the one enquiry that
# returned no_reply_needed -- a correct classification, not a bug -- so it is
# fixtured here to keep it correct.
SETTLED_THREAD = [
    message(
        "m-settled-1",
        "Renter",
        "Hi, could you tell me which floor the flat is on?",
        "2026-09-01T09:00:00",
        subject="Booking enquiry",
        message_status=None,
        route=None,
    ),
    message(
        "m-settled-2",
        "Owner",
        "It's on the second floor.",
        "2026-09-01T09:30:00",
        subject="Booking enquiry",
        message_status=None,
        route=None,
    ),
    message(
        "m-settled-3",
        "Renter",
        "Great, thanks!",
        "2026-09-01T09:40:00",
        subject="Booking enquiry",
        message_status=None,
        route=None,
    ),
]


def service_for(messages, database, registry, answer=HOLDING_TEXT, knowledge=None):
    """A service over a one-thread fixture, with a scripted answer."""
    fake = fake_provider(threads={THREAD_A: thread(THREAD_A, list(messages))})
    model = ScriptedModelProvider([final_response(answer)])

    service = EnquiryReplyService(
        enquiries=fake.enquiries(),
        agent=build_agent(database, registry, model),
        knowledge=knowledge,
    )

    return service, fake, model


def prompt_of(model) -> str:
    return model.conversations[0][0].content


def guidance_of(model) -> dict:
    """The guidance block the prompt actually carried.

    Decoded off the front of what follows the header rather than by splitting
    on a delimiter: the drafting instruction is appended after the JSON, so
    there is no trailing marker to split on.
    """
    tail = prompt_of(model).split("REPLY GUIDANCE (JSON):\n", 1)[1]

    return json.JSONDecoder().raw_decode(tail)[0]


def test_a_business_sensitive_enquiry_still_produces_a_draft(database, registry):
    """The headline regression: an availability question is not a decline.

    Business-sensitive is what an enquiry *is*. Before this, the analyser said
    `reply_needed` and `business_sensitive` and the feature returned nothing.
    """
    service, _fake, model = service_for([ASKS_AVAILABILITY], database, registry)

    draft = service.draft(REF)

    assert draft.message == HOLDING_TEXT
    assert draft.detail == DRAFTED_DETAIL

    # The thread really was business-sensitive -- the test would otherwise be
    # asserting nothing about the divergence at all.
    guidance = guidance_of(model)

    assert guidance["conversation_state"]["business_sensitive"] is True
    assert guidance["conversation_state"]["suggested_outcome"] == "reply_needed"


@pytest.mark.parametrize(
    ("ask", "category"),
    [
        (ASKS_AVAILABILITY, "availability"),
        (ASKS_PRICE, "pricing"),
        (ASKS_AMENITY, "amenity_present"),
    ],
)
def test_an_unanswerable_fact_asks_for_a_holding_reply_not_a_refusal(
    ask,
    category,
    database,
    registry,
):
    """Availability, price and amenity: the three the owner named.

    A scripted model cannot be tested for the words it chooses, so what is
    asserted is everything the application actually controls -- that the
    instruction asks for a holding reply, that the escape hatch is not offered,
    that the holding shape is shown as an example, and that a holding answer
    comes back as a draft rather than being rejected.
    """
    service, _fake, model = service_for([ask], database, registry)

    draft = service.draft(REF)

    assert draft.message == HOLDING_TEXT

    prompt = prompt_of(model)
    guidance = guidance_of(model)

    assert category in guidance["conversation_state"]["business_categories"]

    # Told to write one, and not offered the way out that produced the measured
    # "nothing could be answered safely" on every enquiry.
    assert "holding_replies" in guidance
    assert OPEN_THREAD_INSTRUCTION in prompt
    assert SETTLED_THREAD_INSTRUCTION not in prompt

    # And no new licence to make the fact up. The divergence is about what may
    # be withheld, never about what may be asserted.
    assert "never_invent" in guidance
    assert "never state a price" in guidance["enquiry_drafting_policy"]
    assert guidance["do_not_answer_from_memory"]


def test_approved_knowledge_still_answers_a_parking_enquiry(database, registry):
    """Where a reviewed rule covers the question, it is the answer.

    The holding reply is the fallback for a fact AgentGuard cannot establish --
    not a replacement for one the owner has already written down.
    """
    knowledge = KnowledgeStore(database=database)

    knowledge.create_manual(
        property_slug="renovated-3rd-floor-retreat-3-beds-roslindale-village",
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not allocated to a unit.",
        audience=GUEST_FACING,
        actor_user_id="user-admin-1",
    )

    service, _fake, model = service_for(
        [ASKS_PARKING],
        database,
        registry,
        knowledge=knowledge,
    )

    service.draft(REF)

    guidance = guidance_of(model)

    titles = [rule["title"] for rule in guidance["approved_knowledge"]]

    assert titles == ["Shared parking"]
    assert "Rank 3 of 6" in guidance["approved_knowledge_authority"]

    # The enquiry routing points at the approved rule first, and keeps the
    # holding reply for whatever is left over.
    assert "approved_knowledge" in guidance["business_sensitivity"]["how_to_answer"]


def test_an_open_question_can_never_become_no_reply_needed(database, registry):
    """The analyser owns whether a reply is owed; the model owns only the words.

    A model that answers NO_REPLY_NEEDED over an open question is not being
    conservative -- it produces silence for someone who asked something. When
    the two disagree the analyser wins, and the operator is told a reply is
    owed rather than being handed the "nothing is open" line, which would be
    false.
    """
    service, _fake, _model = service_for(
        [ASKS_AVAILABILITY],
        database,
        registry,
        answer="NO_REPLY_NEEDED",
    )

    draft = service.draft(REF)

    assert draft.message is None
    assert draft.detail == DECLINED_DETAIL
    assert draft.detail != NOTHING_TO_SAY_DETAIL


def test_a_genuine_closing_acknowledgement_still_yields_no_draft(database, registry):
    """Question, answer, "thanks" -- and silence is the right output.

    This is the live thread the investigation looked at, reproduced with
    invented text. Nothing is open, so the escape hatch is offered and honoured.
    """
    service, _fake, model = service_for(
        SETTLED_THREAD,
        database,
        registry,
        answer="NO_REPLY_NEEDED",
    )

    draft = service.draft(REF)

    assert draft.message is None
    assert draft.detail == NOTHING_TO_SAY_DETAIL

    prompt = prompt_of(model)

    assert SETTLED_THREAD_INSTRUCTION in prompt
    assert OPEN_THREAD_INSTRUCTION not in prompt


def test_the_settled_thread_is_settled_by_the_analyser_not_by_a_phrase():
    """Why that thread is closed, stated in flags rather than trusted.

    The bookkeeping cleared the opening question because a later Owner message
    answered it; the last message carries closure evidence and no actionable
    signal. Neither the ref nor any phrase is special-cased anywhere.
    """
    rows = [
        {"sender": row["type"], "message": row["message"], "created_at": None}
        for row in SETTLED_THREAD
    ]

    state = analyse_conversation(rows)

    assert state["suggested_outcome"] == "no_reply_needed"
    assert state["open_signals"] == []
    assert state["latest_guest_message_is_closing"] is True
    assert len(state["answered_earlier_by_us"]) == 1


# -- the booked-guest path is untouched ------------------------------------


BOOKED_ASKS_AVAILABILITY = [
    {
        "sender": "Renter",
        "message": "Are the two nights after ours still available to add on?",
        "created_at": "2026-09-01T09:00:00",
    }
]


def test_booked_guest_business_sensitive_behaviour_is_unchanged():
    """The pipeline that can send reaches exactly the strings it reached before.

    `reply_guidance` has no enquiry branch in it, so this is asserted by value:
    the booked-guest routing and no-reply guidance are the originals, and none
    of the enquiry keys is present.
    """
    guidance = reply_guidance(BOOKED_ASKS_AVAILABILITY)

    state = guidance["conversation_state"]

    assert state["business_sensitive"] is True
    assert state["suggested_outcome"] == "reply_needed"

    assert guidance["business_sensitivity"]["how_to_answer"] == (
        BUSINESS_SENSITIVE_ROUTING
    )
    assert guidance["no_reply_needed"] == NO_REPLY_GUIDANCE

    for key in ("enquiry_drafting_policy", "holding_replies"):
        assert key not in guidance


def test_the_enquiry_guidance_diverges_only_where_it_says_it_does():
    """Four keys replaced, and everything that governs assertion passed through."""
    booked = reply_guidance(BOOKED_ASKS_AVAILABILITY)
    enquiry = enquiry_reply_guidance(BOOKED_ASKS_AVAILABILITY)

    added = set(enquiry) - set(booked)
    changed = {key for key in booked if key in enquiry and enquiry[key] != booked[key]}

    assert added == {"enquiry_drafting_policy", "holding_replies"}
    assert changed == {"no_reply_needed", "business_sensitivity"}

    # The rules about what may be *asserted* are byte-identical.
    for key in (
        "never_invent",
        "authority_order",
        "do_not_answer_from_memory",
        "rules",
        "conversation_state",
        "escalation",
        "approved_knowledge_authority",
    ):
        assert enquiry[key] == booked[key]


def test_no_booked_guest_module_reaches_the_enquiry_guidance():
    """Structural, so the divergence cannot leak onto the path that can send."""
    for name in (
        "app/conversation_refresh.py",
        "app/drafts.py",
        "app/inbox_view.py",
        "app/connectors/lodgify/messaging_tools.py",
    ):
        source = pathlib.Path(name).read_text(encoding="utf-8")

        assert "enquiry_reply_guidance" not in source
        assert "enquiry_drafting_policy" not in source


# -- permissions and the count ---------------------------------------------


def test_drafting_requires_run_agent_not_merely_a_read(enquiry_api):
    """Spending a model call is an execution, whatever it reads.

    A VIEWER can see the enquiry list and cannot drive the model -- the same
    line every other model-execution route in this app draws.
    """
    viewer = enquiry_api.client("VIEWER")

    assert viewer.get("/enquiries").status_code == 200

    response = viewer.post(f"/enquiries/{REF}/reply-draft")

    assert response.status_code == 403

    # Refused before anything ran: no model call was spent on it.
    assert enquiry_api.model.call_count == 0


def test_an_operator_may_draft(enquiry_api):
    response = enquiry_api.client("OPERATOR").post(f"/enquiries/{REF}/reply-draft")

    assert response.status_code == 200


def test_the_list_carries_the_total_so_a_truncated_page_says_so(enquiry_api):
    """ "Showing 20 of 47" needs the 47, and a bounded page cannot report it.

    Without this, twenty rows out of forty-seven and twenty out of twenty look
    identical on screen and an operator believes they have seen the queue.
    """
    reservations = [
        enquiry_row(
            ENQUIRY_ID + index, THREAD_A, created_at=f"2026-09-01T09:{index:02}"
        )
        for index in range(5)
    ]

    enquiry_api.module.lodgify_enquiries = fake_provider(
        reservations=reservations
    ).enquiries()

    body = enquiry_api.client().get("/enquiries", params={"limit": 2}).json()

    assert body["count"] == 2
    assert body["total"] == 5
    assert len(body["enquiries"]) == 2


def test_the_total_equals_the_count_when_nothing_is_truncated(enquiry_api):
    body = enquiry_api.client().get("/enquiries").json()

    assert body["count"] == body["total"] == 1
