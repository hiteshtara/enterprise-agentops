"""Owner-authored cancellation-retention policy.

A cancelled reservation earns a 30% retention offer. Almost every test here is
about the ways that must *not* happen:

  * a guest who asks about the cancellation policy has not cancelled
  * a guest who says they might cancel has not cancelled
  * a guest who asks about a refund has not cancelled
  * a booking state we could not read is not a cancellation
  * the offer goes out once, not on every poll
  * 30% is the only figure, whatever a past reply said
  * the mechanics -- coupon, expiry, fees, blackout dates, stacking -- do not
    exist, so nothing may describe them
  * accepting the offer is a person's job, because AgentGuard has no tool that
    changes a price or restores a reservation

All guest text and all bookings are invented. No test reaches Lodgify or OpenAI.
"""

import pytest

from app.cancellation import (
    RETENTION_DISCOUNT,
    accepts_offer,
    asks_about_mechanics,
    draft_violates_policy,
    is_cancelled,
    offer_already_made,
    outcome_for,
)
from app.drafts import DraftStatus
from app.hospitality import reply_guidance
from app.tool_registry import ToolRisk
from tests.lodgify_fakes import message
from tests.test_proactive_drafting import REF, CountingModel, build

# -- invented guest wording -----------------------------------------------

ASKS_THE_POLICY = "What is your cancellation policy?"

MIGHT_CANCEL = "We might cancel, I'm not sure yet."

ASKS_REFUND = "If we cancelled, what refund would we get?"

WANTS_SHORTER = "Could we shorten the reservation by a night?"

HAS_CANCELLED = "We've had to cancel our trip, sorry about that."

ACCEPTS = "Yes please, we'd like to keep the reservation and take the 30% off."

ASKS_MECHANICS = "How do I redeem the 30% on a future stay? Does it expire?"

ASKS_FEES = "Does the 30% apply to the cleaning fee and taxes too?"

OFFER_DRAFT = (
    "We'd really like the opportunity to keep your business. If you'd like to "
    "keep your reservation, we can offer 30% off your stay. If you still need "
    "to cancel, we completely understand, and we'd be happy to extend the same "
    "30% offer toward your next Boston visit."
)

WRONG_DISCOUNT_DRAFT = "We can offer you 20% off if you keep the reservation."

INVENTED_MECHANICS_DRAFT = (
    "We can offer 30% off -- use coupon code STAY30, valid until December."
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


def owner(text: str, identifier: str = "m-owner-1"):
    return message(identifier, "Owner", text, "2026-09-01T11:00:00")


def cancelled_build(messages, database, agent_factory, answer=OFFER_DRAFT, **kwargs):
    """A conversation whose booking is authoritatively cancelled."""
    return build(
        messages,
        database,
        agent_factory,
        model=CountingModel(answer=answer),
        booking_kwargs={"canceled_at": "2026-09-01T09:00:00"},
        **kwargs,
    )


# -- the trigger is booking state -----------------------------------------


def test_the_cancellation_timestamp_is_the_authoritative_signal():
    assert is_cancelled("Booked", "2026-09-01T09:00:00") is True
    assert is_cancelled("Cancelled", None) is True
    assert is_cancelled("Canceled", None) is True
    assert is_cancelled("Booked", None) is False


def test_unreadable_booking_state_is_unknown_not_cancelled():
    """14: an unreadable status must not hand out a discount, and must not be
    reported as a confident 'not cancelled' either."""
    assert is_cancelled(None, None) is None
    assert is_cancelled("", None) is None


@pytest.mark.parametrize(
    "text", [ASKS_THE_POLICY, MIGHT_CANCEL, ASKS_REFUND, WANTS_SHORTER, HAS_CANCELLED]
)
def test_message_text_alone_never_triggers_the_offer(text):
    """2, 3, 4: not even a guest *saying* they cancelled fires it -- only
    authoritative booking state does."""
    offer, escalate, _reason = outcome_for(
        False, [{"sender": "Renter", "message": text}]
    )

    assert offer is False
    assert escalate is False


@pytest.mark.parametrize("state", [False, None])
def test_an_uncancelled_or_unknown_booking_gets_no_offer(state):
    """14: uncertain booking state behaves like no cancellation, never like one."""
    offer, _escalate, _reason = outcome_for(
        state, [{"sender": "Renter", "message": HAS_CANCELLED}]
    )

    assert offer is False


# -- 1/5. the offer itself -------------------------------------------------


def test_a_confirmed_cancellation_earns_the_offer():
    """1 and 5: cancelled, and not yet offered."""
    offer, escalate, reason = outcome_for(
        True, [{"sender": "Renter", "message": HAS_CANCELLED}]
    )

    assert offer is True
    assert escalate is False
    assert reason


def test_the_model_is_told_the_figure_and_the_boundaries():
    guidance = reply_guidance(
        [{"sender": "Renter", "message": HAS_CANCELLED}],
        booking_cancelled=True,
    )

    policy = guidance["cancellation_policy"]

    assert RETENTION_DISCOUNT in policy
    assert "next Boston visit" in policy
    assert "never any other number" in policy
    assert "coupon code" in policy

    assert guidance["cancellation_state"]["reservation_cancelled"] is True
    assert guidance["cancellation_state"]["make_retention_offer"] is True


def test_an_uncancelled_conversation_is_never_told_about_the_offer():
    """3: a guest asking the policy must not even see the retention wording."""
    guidance = reply_guidance(
        [{"sender": "Renter", "message": ASKS_THE_POLICY}],
        booking_cancelled=False,
    )

    assert "cancellation_policy" not in guidance
    assert "cancellation_state" not in guidance


# -- 6. do not double-offer ------------------------------------------------


def test_an_offer_already_made_is_recognised():
    assert offer_already_made([{"sender": "Owner", "message": OFFER_DRAFT}]) is True
    assert offer_already_made([{"sender": "Renter", "message": OFFER_DRAFT}]) is False
    assert offer_already_made([{"sender": "Owner", "message": "Thanks!"}]) is False


def test_the_offer_is_not_repeated_once_it_has_gone_out():
    """6: still cancelled, but the offer has already been communicated."""
    offer, escalate, _reason = outcome_for(
        True,
        [
            {"sender": "Renter", "message": HAS_CANCELLED},
            {"sender": "Owner", "message": OFFER_DRAFT},
            {"sender": "Renter", "message": "Understood, thank you."},
        ],
    )

    assert offer is False
    assert escalate is False


# -- 7/8. questions nobody can answer yet ---------------------------------


def test_accepting_the_offer_is_a_persons_job():
    """7: AgentGuard has no way to apply a discount or restore a booking."""
    assert accepts_offer(ACCEPTS) is True

    offer, escalate, reason = outcome_for(
        True,
        [
            {"sender": "Renter", "message": HAS_CANCELLED},
            {"sender": "Owner", "message": OFFER_DRAFT},
            {"sender": "Renter", "message": ACCEPTS},
        ],
    )

    assert offer is False
    assert escalate is True
    assert "cannot change a price" in reason


@pytest.mark.parametrize("text", [ASKS_MECHANICS, ASKS_FEES])
def test_asking_how_the_discount_works_escalates(text):
    """8 and 10: none of these terms exist, so none may be answered."""
    assert asks_about_mechanics(text) is True

    _offer, escalate, reason = outcome_for(
        True, [{"sender": "Renter", "message": text}]
    )

    assert escalate is True
    assert reason


def test_an_ordinary_question_does_not_escalate():
    assert asks_about_mechanics("Is there parking?") is False
    assert accepts_offer("Is there parking?") is False


# -- 9/10. what the draft itself may say -----------------------------------


def test_a_draft_naming_another_discount_is_caught():
    """9: a historical reply offering 20% is exactly how a superseded number
    gets back into circulation."""
    reason = draft_violates_policy(WRONG_DISCOUNT_DRAFT)

    assert reason
    assert RETENTION_DISCOUNT in reason


def test_a_draft_inventing_mechanics_is_caught():
    """10: a coupon code and an expiry date the owner never agreed to."""
    assert draft_violates_policy(INVENTED_MECHANICS_DRAFT)


def test_the_approved_wording_passes():
    assert draft_violates_policy(OFFER_DRAFT) is None


# -- end to end ------------------------------------------------------------


def test_a_cancelled_booking_prepares_the_offer(database, agent_factory):
    """1 and 5 end to end."""
    service, _model, _, drafts = cancelled_build(
        [guest(HAS_CANCELLED)], database, agent_factory
    )

    result = service.process(REF)

    assert result.status == DraftStatus.DRAFT_READY.value

    assert RETENTION_DISCOUNT in drafts.current_for(REF).message


@pytest.mark.parametrize("text", [ASKS_MECHANICS, ASKS_FEES, ACCEPTS])
def test_an_unanswerable_question_waits_for_a_person(text, database, agent_factory):
    """7 and 8 end to end."""
    service, _model, _, drafts = cancelled_build([guest(text)], database, agent_factory)

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert drafts.current_for(REF).detail


@pytest.mark.parametrize("answer", [WRONG_DISCOUNT_DRAFT, INVENTED_MECHANICS_DRAFT])
def test_a_non_compliant_draft_never_becomes_a_ready_reply(
    answer, database, agent_factory
):
    """9 and 10 end to end: the model wrote it anyway, and it still cannot go
    out as an ordinary one-click send."""
    service, _model, _, drafts = cancelled_build(
        [guest(HAS_CANCELLED)], database, agent_factory, answer=answer
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert drafts.current_for(REF).status != DraftStatus.DRAFT_READY.value


def test_an_uncancelled_booking_prepares_an_ordinary_reply(database, agent_factory):
    """2, 3 and 4 end to end: the guest gets an answer, not a discount."""
    service, _model, _, drafts = build(
        [guest(ASKS_THE_POLICY)],
        database,
        agent_factory,
        model=CountingModel(answer="Our cancellation terms are on your booking."),
    )

    result = service.process(REF)

    assert result.status == DraftStatus.DRAFT_READY.value
    assert RETENTION_DISCOUNT not in drafts.current_for(REF).message


# -- 11/12/13. what AgentGuard structurally cannot do ----------------------


def test_agentguard_has_no_tool_that_changes_a_price_or_a_reservation(database):
    """11 and 12: the guarantee is the absence of a capability, not a rule the
    model is asked to follow. Nothing registered can mutate a booking."""
    from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools
    from tests.lodgify_fakes import THREAD_A, FakeLodgify, booking, thread

    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        threads={THREAD_A: thread(THREAD_A, [guest(HAS_CANCELLED)])},
    )

    registry = build_registry(database, LodgifyMessagingTools(fake.inbox()))

    names = {tool["name"] for tool in registry.describe()}

    for forbidden in (
        "cancel",
        "restore",
        "reinstate",
        "price",
        "pricing",
        "discount",
        "refund",
        "quote",
        "booking",
        "reservation",
    ):
        assert not any(forbidden in name for name in names), forbidden

    # Every guest-facing tool is a read except the one that sends a message,
    # and that one is DANGEROUS and approval-gated. `restart_migration` belongs
    # to the unrelated demo domain and touches no reservation.
    guest_tools = [tool for tool in registry.describe() if "guest" in tool["name"]]

    writes = [tool for tool in guest_tools if tool["risk"] != ToolRisk.READ.value]

    assert [tool["name"] for tool in writes] == ["send_guest_reply"]
    assert writes[0]["risk"] == ToolRisk.DANGEROUS.value


def build_registry(database, lodgify_messaging):
    from app.migration_store import MigrationBatchStore
    from app.tool_setup import build_tool_registry

    return build_tool_registry(
        migration_store=MigrationBatchStore(database=database),
        lodgify_messaging=lodgify_messaging,
    )


@pytest.mark.parametrize("text", [HAS_CANCELLED, ACCEPTS, ASKS_MECHANICS])
def test_nothing_is_ever_sent_automatically(text, database, agent_factory):
    """13: in every branch, preparing a reply is not sending one."""
    service, _model, fake, _ = cancelled_build([guest(text)], database, agent_factory)

    service.process(REF)
    service.process(REF, force=True)

    assert fake.posts == []
