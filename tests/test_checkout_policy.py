"""Owner-authored late-checkout policy.

Three facts may not vary, whatever a model or a past reply says:

    standard checkout                 10:00 AM
    automatic late-checkout ceiling   11:00 AM
    anything past 11:00 AM            the owner's decision

The tests that matter most are the negative ones. A model that promises 1 PM,
and a historical reply in which the owner once offered noon, must both fail to
put a later time in front of a guest as an ordinary ready-to-send draft.

All guest text is invented. No test reaches Lodgify or OpenAI.
"""

import pytest

from app.drafts import DraftStatus
from app.hospitality import analyse_conversation, reply_guidance
from app.late_checkout import (
    AUTOMATIC_CEILING,
    CEILING_MINUTES,
    STANDARD_CHECKOUT,
    exceeds_ceiling,
    is_late_checkout_request,
    latest_checkout_time,
)
from tests.lodgify_fakes import message
from tests.test_proactive_drafting import REF, CountingModel, build

# -- invented guest wording -----------------------------------------------

ASKS_VAGUELY = "Can I get late checkout?"

ASKS_FOR_ELEVEN = "Can we stay until 11?"

ASKS_A_LITTLE_LATER = "Can we stay a little later?"

ASKS_FOR_NOON = "Can we stay until noon?"

ASKS_FOR_ONE_PM = "Can we stay until 1 PM?"

COMPLIANT_DRAFT = "We can extend checkout until 11:00 AM for you."

OVER_CEILING_DRAFT = "Sure, you can check out at 1 PM."


def guest(text: str, identifier: str = "m-guest-1"):
    return message(
        identifier,
        "Renter",
        text,
        "2026-09-01T10:00:00",
        message_status=None,
        route=None,
    )


def owner(text: str, identifier: str = "m-owner-1"):
    return message(identifier, "Owner", text, "2026-09-01T11:00:00")


# -- 1. the numbers themselves --------------------------------------------


def test_the_policy_states_the_owners_two_times():
    assert STANDARD_CHECKOUT == "10:00 AM"
    assert AUTOMATIC_CEILING == "11:00 AM"
    assert CEILING_MINUTES == 11 * 60


@pytest.mark.parametrize(
    "text",
    [
        ASKS_VAGUELY,
        ASKS_FOR_ELEVEN,
        ASKS_A_LITTLE_LATER,
        ASKS_FOR_NOON,
        ASKS_FOR_ONE_PM,
    ],
)
def test_every_phrasing_is_recognised_as_a_late_checkout_request(text):
    assert is_late_checkout_request(text) is True


# -- 2/3/4/5. where the ceiling falls --------------------------------------


@pytest.mark.parametrize("text", [ASKS_VAGUELY, ASKS_FOR_ELEVEN, ASKS_A_LITTLE_LATER])
def test_a_request_within_policy_needs_no_owner_decision(text):
    """1, 2 and 5: the extra hour is pre-approved, so nothing escalates."""
    assert exceeds_ceiling(text) is False

    state = analyse_conversation([{"sender": "Renter", "message": text}])

    assert state["late_checkout_requested"] is True
    assert state["owner_approval_required"] is False


@pytest.mark.parametrize("text", [ASKS_FOR_NOON, ASKS_FOR_ONE_PM])
def test_a_request_past_the_ceiling_is_the_owners_decision(text):
    """3 and 4: noon and 1 PM are both past what may be offered on our own."""
    assert exceeds_ceiling(text) is True

    state = analyse_conversation([{"sender": "Renter", "message": text}])

    assert state["owner_approval_required"] is True
    assert state["owner_approval_reason"]


@pytest.mark.parametrize(
    "text,minutes",
    [
        ("can we check out at 11?", 11 * 60),
        ("can we check out at 11:30?", 11 * 60 + 30),
        ("can we check out at noon?", 12 * 60),
        ("can we check out at 12:30 pm?", 12 * 60 + 30),
        ("can we check out at 1?", 13 * 60),
        ("can we check out at 1 pm?", 13 * 60),
        ("can we check out at 13:00?", 13 * 60),
        ("can we leave at 10:30 am?", 10 * 60 + 30),
    ],
)
def test_times_are_read_the_way_a_guest_means_them(text, minutes):
    """A bare hour in a checkout sentence resolves towards the afternoon, which
    is both how guests speak and the cautious reading."""
    assert latest_checkout_time(text) == minutes


def test_an_arrival_time_is_never_read_as_a_checkout_time():
    """ "Check-in is from 3 PM" must not escalate a late-checkout decision."""
    assert latest_checkout_time("What time is check-in? Is 3 PM right?") is None
    assert exceeds_ceiling("We arrive at 4 PM") is False


def test_a_message_about_nothing_relevant_names_no_time():
    assert latest_checkout_time("Is there parking?") is None
    assert is_late_checkout_request("Is there parking?") is False


# -- what the model is told ------------------------------------------------


def test_the_model_is_given_the_ceiling_and_the_escalation():
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_FOR_ONE_PM}])

    policy = guidance["late_checkout_policy"]

    assert "10:00 AM" in policy
    assert "11:00 AM" in policy

    # Stated separately from the conversation block so it cannot be missed.
    assert guidance["owner_approval_required"]


def test_owner_approved_knowledge_outranks_a_historical_example():
    """6: the authority order is what stops an old reply becoming policy."""
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_VAGUELY}])

    order = " ".join(guidance["authority_order"])

    assert order.index("OWNER-APPROVED") < order.index("HISTORICAL EXAMPLES")
    assert "never facts" in order


# -- 3/4/7. what actually reaches a draft ---------------------------------


def test_a_request_within_policy_produces_an_ordinary_draft(database, agent_factory):
    """1, 2 and 5 end to end: a normal request needs no owner decision."""
    service, _model, _, drafts = build(
        [guest(ASKS_VAGUELY)],
        database,
        agent_factory,
        model=CountingModel(answer=COMPLIANT_DRAFT),
    )

    result = service.process(REF)

    assert result.status == DraftStatus.DRAFT_READY.value

    assert drafts.current_for(REF).message == COMPLIANT_DRAFT


@pytest.mark.parametrize("text", [ASKS_FOR_NOON, ASKS_FOR_ONE_PM])
def test_a_request_past_the_ceiling_offers_eleven_and_waits_for_the_owner(
    text, database, agent_factory
):
    """3 and 4: the guest is not left without an answer -- the reply offering
    11:00 AM is prepared -- but it is the owner who releases it."""
    service, _model, _, drafts = build(
        [guest(text)],
        database,
        agent_factory,
        model=CountingModel(answer=COMPLIANT_DRAFT),
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value

    draft = drafts.current_for(REF)

    assert "11:00 AM" in draft.message
    assert draft.detail


def test_the_model_cannot_promise_a_later_time_than_policy_allows(
    database, agent_factory
):
    """7: the prompt asks; this is what happens when the model ignores it."""
    service, _model, _, drafts = build(
        [guest(ASKS_VAGUELY)],
        database,
        agent_factory,
        model=CountingModel(answer=OVER_CEILING_DRAFT),
    )

    result = service.process(REF)

    # Never DRAFT_READY: an over-ceiling promise may not become an ordinary
    # one-click send.
    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value

    assert drafts.current_for(REF).status != DraftStatus.DRAFT_READY.value


def test_a_historical_reply_offering_noon_does_not_become_a_promise(
    database, agent_factory
):
    """6 end to end: even if a past reply offered noon and the model copies it,
    the ceiling is enforced on the text rather than trusted to the prompt."""
    service, _model, _, drafts = build(
        [guest(ASKS_VAGUELY)],
        database,
        agent_factory,
        model=CountingModel(answer="Last time we let guests check out at noon."),
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert drafts.current_for(REF).status != DraftStatus.DRAFT_READY.value


# -- 8. a promise already made to this guest -------------------------------


def test_an_owner_promise_in_this_thread_still_outranks_the_general_policy():
    """8: retracting a promise costs more trust than the inconsistency does."""
    messages = [
        {"sender": "Renter", "message": ASKS_FOR_ONE_PM},
        {"sender": "Owner", "message": "Yes, you can check out at 1 PM."},
        {"sender": "Renter", "message": "Great, thank you!"},
    ]

    guidance = reply_guidance(messages)

    order = " ".join(guidance["authority_order"])

    assert order.index("COMMITMENT ALREADY MADE") < order.index("OWNER-APPROVED")

    # And the thread is not still escalating: the open request was answered.
    assert guidance["conversation_state"]["owner_approval_required"] is False


def test_an_answered_request_stops_escalating():
    """Escalation is computed from what is still open, so a request the owner
    already answered does not hold the thread open forever."""
    messages = [
        {"sender": "Renter", "message": ASKS_FOR_ONE_PM},
        {"sender": "Owner", "message": "I'll check and come back to you."},
    ]

    assert analyse_conversation(messages)["owner_approval_required"] is False


# -- 9/10. nothing else changed -------------------------------------------


def test_no_reply_needed_behaviour_is_unchanged(database, agent_factory):
    """9: a closing conversation still costs nothing and drafts nothing."""
    service, model, _, drafts = build(
        [
            guest("Is there parking?"),
            owner("Yes, shared out front."),
            guest("Thank you!", "m-guest-2"),
        ],
        database,
        agent_factory,
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NO_REPLY_NEEDED.value
    assert model.model_calls == 0
    assert drafts.current_for(REF).message is None


@pytest.mark.parametrize("text", [ASKS_VAGUELY, ASKS_FOR_NOON, ASKS_FOR_ONE_PM])
def test_nothing_is_ever_sent_automatically(text, database, agent_factory):
    """10: preparing a reply is not sending one, in every branch."""
    service, _model, fake, _ = build(
        [guest(text)],
        database,
        agent_factory,
        model=CountingModel(answer=COMPLIANT_DRAFT),
    )

    service.process(REF)
    service.process(REF, force=True)

    assert fake.posts == []
