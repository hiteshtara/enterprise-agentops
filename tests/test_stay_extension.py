"""Stay extension: detected, escalated, and deliberately never computed.

A guest asks to keep the home for another night, to add days, or to move their
dates. AgentGuard recognises that and hands it to the owner. It does not work
out whether the nights can be sold, and these tests are mostly about what it
does *not* do:

    no availability read        the live calendar is never asked
    no booking-overlap scan     the archive is never walked for this
    no second model call        no date is extracted from the thread
    no verdict                  nothing free / occupied / unknown is computed

That is a scope decision. Whether a night can be sold is only half the
question; whether the owner wants to sell it is the other half, and no lookup
answers that. So the whole apparatus was removed rather than tuned, and an open
request takes the same NEEDS_HUMAN_REVIEW path every other escalation takes.

All guest text, all bookings and all dates are invented. No test opens a
socket, no test uses a real credential, and no test touches the development
database.
"""

import json

import pytest

from app.drafts import DraftStatus
from app.hospitality import analyse_conversation, reply_guidance
from app.stay_extension import (
    ESCALATION_REASON,
    POLICY_GUIDANCE,
    is_extension_request,
    requested_in,
)
from tests.lodgify_fakes import message
from tests.test_proactive_drafting import REF, CountingModel, build

# -- invented reservation and wording --------------------------------------

ARRIVAL = "2026-03-06"

DEPARTURE = "2026-03-10"

ASKS_FOR_ANOTHER_NIGHT = "Any chance we could stay one more night?"

ASKS_TO_EXTEND = "We would like to extend our reservation by two nights."

ASKS_ABOUT_PARKING = "Is there parking on the street outside?"

ESCALATED_DRAFT = (
    "Let me check those dates with the owner and come straight back to you."
)

PARKING_DRAFT = "Parking is shared out front."


def guest_message(
    text: str,
    identifier: str = "m-guest-1",
    at: str = "2026-03-01T10:00:00",
):
    return message(
        identifier,
        "Renter",
        text,
        at,
        message_status=None,
        route=None,
    )


def owner_message(text: str, identifier: str = "m-owner-1"):
    return message(identifier, "Owner", text, "2026-03-01T11:00:00")


def extension_service(
    database,
    agent_factory,
    messages=None,
    draft=ESCALATED_DRAFT,
):
    """A refresh service over an invented thread asking for another night."""
    return build(
        messages if messages is not None else [guest_message(ASKS_FOR_ANOTHER_NIGHT)],
        database,
        agent_factory,
        model=CountingModel(answer=draft),
        booking_kwargs={"arrival": ARRIVAL, "departure": DEPARTURE},
    )


# -- detection -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        ASKS_FOR_ANOTHER_NIGHT,
        ASKS_TO_EXTEND,
        "Could we add an extra night at the end?",
        "Is it possible to extend until Friday?",
        "We would love to stay another night if the home is free.",
        "Can we book one additional night?",
    ],
)
def test_extension_wording_is_recognised(text):
    assert is_extension_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        ASKS_ABOUT_PARKING,
        "What time is checkout?",
        "Could we have a late checkout on the last day?",
        "The extended forecast looks like rain all week.",
        "",
        None,
    ],
)
def test_unrelated_wording_is_not_an_extension_request(text):
    assert is_extension_request(text) is False


def test_requested_in_reads_the_open_messages():
    assert (
        requested_in(
            [
                {"sender": "Renter", "message": ASKS_ABOUT_PARKING},
                {"sender": "Renter", "message": ASKS_FOR_ANOTHER_NIGHT},
            ]
        )
        is True
    )

    assert requested_in([{"sender": "Renter", "message": ASKS_ABOUT_PARKING}]) is False
    assert requested_in([]) is False
    assert requested_in(None) is False


def test_an_open_extension_request_is_reported_as_a_fact():
    state = analyse_conversation(
        [{"sender": "Renter", "message": ASKS_FOR_ANOTHER_NIGHT}]
    )

    assert state["stay_extension_requested"] is True


def test_an_answered_extension_request_does_not_fire_again():
    """The same mechanism every other topic uses: only guest messages that
    arrived after our last reply can still be open."""
    messages = [
        {"sender": "Renter", "message": ASKS_FOR_ANOTHER_NIGHT},
        {"sender": "Owner", "message": "I'll check those dates and confirm."},
    ]

    assert analyse_conversation(messages)["stay_extension_requested"] is False


# -- guidance: conditional, and promising nothing ---------------------------


def test_the_policy_is_attached_only_while_the_request_is_open():
    """A rule says how to answer a topic if it is open. It is never a checklist,
    and a thread that once mentioned extra nights must not keep answering."""
    open_thread = reply_guidance([{"sender": "Renter", "message": ASKS_TO_EXTEND}])

    assert "stay_extension_policy" in open_thread
    assert "Never introduce a topic" in open_thread["topic_rules_are_conditional"]

    answered = reply_guidance(
        [
            {"sender": "Renter", "message": ASKS_TO_EXTEND},
            {"sender": "Owner", "message": "I'll look at those dates and confirm."},
        ]
    )

    assert "stay_extension_policy" not in answered


def test_no_verdict_or_calendar_state_is_published_with_the_guidance():
    """The whole apparatus is gone, not merely unused. There is no block for a
    verdict, a night state or a requested window to travel in."""
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_TO_EXTEND}])

    assert "stay_extension_state" not in guidance

    rendered = json.dumps(guidance)

    for absent in ("night_state", "requested_nights", "requested_checkout"):
        assert absent not in rendered


def test_the_guidance_never_promises_quotes_availability_or_claims_a_change():
    wording = f"{POLICY_GUIDANCE} {ESCALATION_REASON}".lower()

    # What it must forbid, in so many words.
    assert "never say the stay has been extended" in wording
    assert "never quote availability" in wording
    assert "does not check the calendar" in wording
    assert "only the owner can decide" in wording
    assert "check the dates with the owner" in wording

    # And what it must never itself say. Every one of these either promises the
    # extension, forecloses it, or reports a calendar nothing here read.
    for forbidden in (
        "may be available",
        "appears possible",
        "looks like the additional night",
        "those nights are booked",
        "currently booked",
        "another reservation",
        "cancel",
        "check again closer",
        "we have confirmed",
        "has been added",
    ):
        assert forbidden not in wording, f"{forbidden!r} says more than we know"


def test_an_open_request_marks_the_guidance_as_needing_the_owner():
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_TO_EXTEND}])

    # The top-level key, which is where every escalation is stated so it
    # cannot be missed in a long conversation_state block.
    assert guidance["owner_approval_required"] == ESCALATION_REASON


# -- authority: a commitment already made in this thread wins ---------------


def test_a_commitment_already_made_in_this_thread_wins():
    """The owner promised the extra night in this thread. General policy must
    not walk that back, and the existing exception mechanism is what says so."""
    messages = [
        {"sender": "Renter", "message": ASKS_FOR_ANOTHER_NIGHT},
        {
            "sender": "Owner",
            "message": "Yes, you can keep the place another night -- those dates are yours.",
        },
        {"sender": "Renter", "message": "Wonderful, see you then!"},
    ]

    approved = [
        {
            "topic": "availability",
            "content": "Extra nights are never confirmed without the owner.",
        }
    ]

    guidance = reply_guidance(messages, approved)

    order = " ".join(guidance["authority_order"])

    assert order.index("COMMITMENT ALREADY MADE") < order.index("OWNER-APPROVED")

    exceptions = guidance["current_conversation_exceptions"]

    assert exceptions
    assert exceptions[0]["marker"] == "CURRENT_CONVERSATION_EXCEPTION"

    # And nothing re-opens the topic: the owner already answered it.
    assert guidance["conversation_state"]["stay_extension_requested"] is False
    assert "stay_extension_policy" not in guidance


# -- escalation: the whole of what the runtime does about it ----------------


def test_an_open_extension_request_needs_human_review(database, agent_factory):
    service, _model, _fake, drafts = extension_service(database, agent_factory)

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value

    draft = drafts.current_for(REF)

    assert ESCALATION_REASON in draft.detail
    # The text is still prepared -- escalation is a decision about who sends it,
    # never a reason to leave the owner with a blank box.
    assert draft.message == ESCALATED_DRAFT


def test_an_answered_extension_request_does_not_escalate_again(database, agent_factory):
    """The owner already replied about the nights; the open question is parking.
    Nothing may re-fire the extension escalation on a later state."""
    service, _model, _fake, drafts = extension_service(
        database,
        agent_factory,
        messages=[
            guest_message(ASKS_FOR_ANOTHER_NIGHT),
            owner_message("I'll check those dates and confirm."),
            guest_message(
                ASKS_ABOUT_PARKING,
                identifier="m-guest-2",
                at="2026-03-01T12:00:00",
            ),
        ],
        draft=PARKING_DRAFT,
    )

    result = service.process(REF)

    assert result.status == DraftStatus.DRAFT_READY.value
    assert ESCALATION_REASON not in (drafts.current_for(REF).detail or "")


# -- the negative property: nothing is looked up ----------------------------


def test_no_availability_call_is_made_for_an_extension_request(database, agent_factory):
    """The live calendar is not consulted. Zero requests, not "few"."""
    service, _model, fake, _drafts = extension_service(database, agent_factory)

    service.process(REF)
    service.process(REF, force=True)

    assert fake.availability_reads == []


def test_an_extension_costs_no_extra_booking_archive_walk(database, agent_factory):
    """No booking-overlap scan. An extension thread reads the archive exactly as
    often as an ordinary question does -- the resolution every refresh needs,
    and nothing on top of it."""
    extension, _m1, extension_fake, _d1 = extension_service(database, agent_factory)

    extension.process(REF)

    ordinary, _m2, ordinary_fake, _d2 = extension_service(
        database,
        agent_factory,
        messages=[guest_message(ASKS_ABOUT_PARKING)],
        draft=PARKING_DRAFT,
    )

    ordinary.process(REF, force=True)

    assert len(extension_fake.booking_reads) == len(ordinary_fake.booking_reads)


def test_an_extension_costs_no_extra_model_call(database, agent_factory):
    """There is no date-extraction call any more. One conversation state, one
    drafting run, exactly as for any other topic."""
    extension, extension_model, _f1, _d1 = extension_service(database, agent_factory)

    extension.process(REF)

    ordinary, ordinary_model, _f2, _d2 = extension_service(
        database,
        agent_factory,
        messages=[guest_message(ASKS_ABOUT_PARKING)],
        draft=PARKING_DRAFT,
    )

    ordinary.process(REF, force=True)

    assert extension_model.model_calls == ordinary_model.model_calls


def test_the_drafting_prompt_carries_no_calendar_state(database, agent_factory):
    """Nothing was established, so nothing about the nights may travel to the
    model -- not a verdict, not a night, not a window."""
    service, _model, _fake, drafts = extension_service(database, agent_factory)

    service.process(REF)

    rendered = json.dumps(drafts.current_for(REF).to_dict())

    for absent in ("LIVE STAY EXTENSION STATE", "night_state", "occupied", "free"):
        assert absent not in rendered


# -- nothing acts ----------------------------------------------------------


def test_no_reservation_is_modified_and_nothing_is_sent(database, agent_factory):
    service, _model, fake, _drafts = extension_service(database, agent_factory)

    service.process(REF)
    service.process(REF, force=True)

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)
