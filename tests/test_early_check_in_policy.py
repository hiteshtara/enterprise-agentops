"""Owner-authored early check-in policy.

Standard check-in is 4:00 PM, and early access is never promised because a guest
asked. What makes it possible is a fact to look up rather than reason about: is
another guest checking out of this property on the arrival day?

Three states, kept distinct all the way to the screen:

    someone is checking out   -> decline; the turnover needs the day
    nobody is                 -> may be possible; the owner decides the time
    we could not find out     -> say so; the owner decides

The tests that matter most are the ones about the third. A provider failure that
collapsed into "nobody is checking out" would turn an outage into a promise of
early access on a turnover day.

All guest text and all bookings are invented. No test reaches Lodgify or OpenAI.
"""

import pytest

from app.drafts import DraftStatus
from app.early_check_in import (
    STANDARD_CHECK_IN,
    is_early_check_in_request,
    outcome_for,
)
from app.hospitality import analyse_conversation, reply_guidance
from tests.lodgify_fakes import THREAD_A, FakeLodgify, booking, message, thread
from tests.test_proactive_drafting import REF, CountingModel, build

# -- invented guest wording -----------------------------------------------

ASKS_EARLY = "Is early check-in possible?"

ASKS_TO_ARRIVE_EARLY = "Could we arrive early and drop our bags?"

ARRIVAL = "2026-11-25"

# The reply the model is scripted to produce. Deliberately says "may be" and
# names no time, because naming one is the owner's call.
POSSIBLE_DRAFT = (
    "It looks like early check-in may be possible. Let me confirm the schedule "
    "and I'll get back to you."
)

DECLINE_DRAFT = (
    "We have a guest checking out that day and need the full turnover time, so "
    "we won't be able to offer early check-in. Regular check-in is at 4:00 PM."
)


def guest(text: str, identifier: str = "m-guest-1"):
    return message(
        identifier,
        "Renter",
        text,
        "2026-09-01T10:00:00",
        message_status=None,
        route=None,
    )


def build_with_turnover(
    messages,
    database,
    agent_factory,
    same_day_checkout: bool,
    answer: str = POSSIBLE_DRAFT,
    **kwargs,
):
    """A refresh service whose booking archive does or does not hold a turnover.

    The other stay is a real booking row at the same property whose departure
    lands on this guest's arrival day. Nothing about it is ever read beyond
    those two fields plus its status.
    """
    others = (
        [booking(2002, "thread-other", arrival="2026-11-20", departure=ARRIVAL)]
        if same_day_checkout
        else []
    )

    return build(
        messages,
        database,
        agent_factory,
        model=CountingModel(answer=answer),
        extra_bookings=others,
        **kwargs,
    )


# -- 1. the standard time -------------------------------------------------


def test_standard_check_in_is_four_pm():
    assert STANDARD_CHECK_IN == "4:00 PM"


@pytest.mark.parametrize(
    "text",
    [
        ASKS_EARLY,
        ASKS_TO_ARRIVE_EARLY,
        "Can we check in early?",
        "What time can we arrive? Could we come early?",
        "Is an early arrival possible?",
    ],
)
def test_every_phrasing_is_recognised_as_an_early_check_in_request(text):
    assert is_early_check_in_request(text) is True


def test_an_unrelated_question_is_not_an_early_check_in_request():
    assert is_early_check_in_request("Is there parking?") is False
    assert is_early_check_in_request("Can we have a late checkout?") is False


# -- the three states -----------------------------------------------------


def test_a_turnover_day_is_the_one_branch_we_may_answer_ourselves():
    verdict, reason, needs_owner = outcome_for(True)

    assert verdict == "declined"
    assert needs_owner is False
    assert reason


@pytest.mark.parametrize("same_day", [False, None])
def test_anything_that_opens_the_door_is_the_owners_decision(same_day):
    """4 and 5: 'may be possible' and 'we could not check' both escalate. The
    assistant may never choose the early time itself."""
    verdict, reason, needs_owner = outcome_for(same_day)

    assert needs_owner is True
    assert verdict in ("possible", "unknown")
    assert reason


def test_unknown_is_never_treated_as_no_checkout():
    """5 and 14: the failure mode this three-valued answer exists to prevent."""
    assert outcome_for(None)[0] == "unknown"
    assert outcome_for(None)[0] != outcome_for(False)[0]


# -- 2. far in the future -------------------------------------------------


def test_a_far_future_request_promises_nothing_and_explains_why():
    """2: no lookup was made, so the honest answer is that it depends on that
    day's checkout and will be confirmed nearer the time."""
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_EARLY}])

    turnover = guidance["arrival_day_turnover"]

    assert turnover["early_check_in"] == "unknown"
    assert "confirm closer to arrival" in turnover["how_to_answer"]
    assert "4:00 PM" in turnover["how_to_answer"]

    # Never a promise, and never an invented time.
    assert "will be available" not in turnover["how_to_answer"]


# -- what the model is told ------------------------------------------------


def test_the_model_is_told_it_may_not_choose_the_time():
    """8: the instruction exists as well as the runtime enforcement."""
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_EARLY}])

    policy = guidance["early_check_in_policy"]

    assert "4:00 PM" in policy
    assert "never name an earlier time yourself" in policy
    assert "do not blame the cleaners" in policy


def test_the_model_is_told_never_to_read_an_outage_as_a_free_day():
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_EARLY}])

    assert "there is no checkout" in guidance["early_check_in_policy"]


def test_owner_approved_policy_outranks_a_historical_example():
    """6 and 7: an old reply guaranteeing early check-in, or offering 10 AM, is
    style and precedent only -- never a current fact."""
    guidance = reply_guidance([{"sender": "Renter", "message": ASKS_EARLY}])

    order = " ".join(guidance["authority_order"])

    assert order.index("OWNER-APPROVED") < order.index("HISTORICAL EXAMPLES")
    assert "never facts" in guidance["authority_order"][4]

    caveat = guidance["historical_examples_caveat"]

    assert "Never carry over" in caveat
    assert "promise from an example" in caveat


# -- 9. a promise already made to this guest -------------------------------


def test_an_owner_promise_in_this_thread_still_stands():
    """9: the current-conversation exception is unchanged by this policy."""
    messages = [
        {"sender": "Renter", "message": ASKS_EARLY},
        {"sender": "Owner", "message": "Yes, you can check in at 1 PM that day."},
        {"sender": "Renter", "message": "Perfect, thanks!"},
    ]

    guidance = reply_guidance(messages)

    order = " ".join(guidance["authority_order"])

    assert order.index("COMMITMENT ALREADY MADE") < order.index("OWNER-APPROVED")

    # The commitment is surfaced rather than left for the model to notice.
    assert guidance["current_conversation_exceptions"] is not None


# -- 3/4/5. what actually reaches a draft ---------------------------------


def test_a_turnover_day_declines_without_needing_the_owner(database, agent_factory):
    """3: declining is a statement of existing policy, so it is an ordinary
    draft -- which still cannot reach a guest without an approval."""
    service, _model, _, drafts = build_with_turnover(
        [guest(ASKS_EARLY)],
        database,
        agent_factory,
        same_day_checkout=True,
        answer=DECLINE_DRAFT,
    )

    result = service.process(REF)

    assert result.status == DraftStatus.DRAFT_READY.value

    draft = drafts.current_for(REF)

    assert "4:00 PM" in draft.message


def test_a_free_day_is_never_an_automatic_promise(database, agent_factory):
    """4: nobody is checking out, and it still waits for the owner."""
    service, _model, _, drafts = build_with_turnover(
        [guest(ASKS_EARLY)],
        database,
        agent_factory,
        same_day_checkout=False,
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value

    draft = drafts.current_for(REF)

    # The guest is not left without an answer -- the wording exists, it just
    # does not go out on its own.
    assert draft.message == POSSIBLE_DRAFT
    assert draft.detail


def test_an_unreadable_arrival_date_escalates_rather_than_guesses(
    database, agent_factory
):
    """5 and 14: without an arrival date the schedule cannot be established, so
    the answer is unknown -- and unknown is the owner's decision, not a free
    day."""
    service, _model, _, drafts = build(
        [guest(ASKS_EARLY)],
        database,
        agent_factory,
        model=CountingModel(answer=POSSIBLE_DRAFT),
        booking_kwargs={"arrival": ""},
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert drafts.current_for(REF).detail


def test_a_failing_turnover_lookup_escalates(database, agent_factory):
    """A provider failure during the lookup is the same unknown. It must never
    surface as 'nobody is checking out'."""
    service, _model, _, drafts = build_with_turnover(
        [guest(ASKS_EARLY)],
        database,
        agent_factory,
        same_day_checkout=False,
    )

    def explode(_conversation_ref):
        raise RuntimeError("provider unavailable")

    service._inbox.turnover_for = explode

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert drafts.current_for(REF).detail


# -- 11/12. no other guest's data ------------------------------------------


def test_the_turnover_answer_carries_nothing_about_the_other_guest():
    """11: the determination reads two dates and a status off another booking
    and returns one boolean. Identity, contact details, financials and the
    reservation id of that stay are never read, so none can leak."""
    fake = FakeLodgify(
        bookings=[
            booking(1001, THREAD_A, arrival=ARRIVAL, departure="2026-11-29"),
            booking(2002, "thread-other", arrival="2026-11-20", departure=ARRIVAL),
        ],
        threads={THREAD_A: thread(THREAD_A, [guest(ASKS_EARLY)])},
    )

    result = fake.inbox().turnover_for(REF)

    assert result["same_day_checkout"] is True
    assert result["arrival_date"] == ARRIVAL

    # Exactly four keys, and the only date is this guest's own arrival.
    assert set(result) == {
        "conversation_ref",
        "arrival_date",
        "same_day_checkout",
        "reason",
    }

    rendered = repr(result)

    for leak in ("Fixture Guest", "example.invalid", "+15550000000", "2002", "1234.56"):
        assert leak not in rendered


def test_only_a_confirmed_stay_occupies_the_arrival_day():
    """12: an enquiry nobody accepted is not a checkout, so it must not deny a
    guest early access. Authoritative current state, not message text."""
    fake = FakeLodgify(
        bookings=[
            booking(1001, THREAD_A, arrival=ARRIVAL, departure="2026-11-29"),
            booking(
                2002,
                "thread-other",
                arrival="2026-11-20",
                departure=ARRIVAL,
                status="Declined",
            ),
        ],
        threads={THREAD_A: thread(THREAD_A, [guest(ASKS_EARLY)])},
    )

    assert fake.inbox().turnover_for(REF)["same_day_checkout"] is False


def test_a_departure_at_another_property_is_not_this_guests_turnover():
    fake = FakeLodgify(
        bookings=[
            booking(1001, THREAD_A, arrival=ARRIVAL, departure="2026-11-29"),
            booking(
                2002,
                "thread-other",
                property_id=999999,
                arrival="2026-11-20",
                departure=ARRIVAL,
            ),
        ],
        threads={THREAD_A: thread(THREAD_A, [guest(ASKS_EARLY)])},
    )

    assert fake.inbox().turnover_for(REF)["same_day_checkout"] is False


def test_this_guests_own_booking_is_not_its_own_turnover():
    """A stay whose departure happens to equal its own arrival must not make a
    guest their own blocker."""
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A, arrival=ARRIVAL, departure=ARRIVAL)],
        threads={THREAD_A: thread(THREAD_A, [guest(ASKS_EARLY)])},
    )

    assert fake.inbox().turnover_for(REF)["same_day_checkout"] is False


# -- 10. nothing sends -----------------------------------------------------


@pytest.mark.parametrize("same_day", [True, False])
def test_nothing_is_ever_sent_automatically(same_day, database, agent_factory):
    service, _model, fake, _ = build_with_turnover(
        [guest(ASKS_EARLY)],
        database,
        agent_factory,
        same_day_checkout=same_day,
    )

    service.process(REF)
    service.process(REF, force=True)

    assert fake.posts == []


# -- the analysis surfaces it ---------------------------------------------


def test_the_open_request_is_reported_as_a_fact_not_a_hint():
    state = analyse_conversation([{"sender": "Renter", "message": ASKS_EARLY}])

    assert state["early_check_in_requested"] is True


def test_an_answered_request_stops_escalating():
    messages = [
        {"sender": "Renter", "message": ASKS_EARLY},
        {"sender": "Owner", "message": "I'll check the schedule and confirm."},
    ]

    assert analyse_conversation(messages)["early_check_in_requested"] is False
