"""Conversation-state reasoning for guest reply drafting.

These are the tests for the bug that produced this module's newest half: a live
draft answered a wine-opener question the host had already answered, because the
guidance was indexed by topic and had no idea what was still open.

The bookkeeping half is deterministic and tested directly. The wording half
belongs to the model, so what is asserted there is that the *rules reach it* --
that the guidance says what it must say. A rule that quietly disappears from the
bundle is the failure mode worth catching.
"""

import json

import pytest

from app.hospitality import (
    NO_REPLY_NEEDED,
    analyse_conversation,
    is_acknowledgement,
    reply_guidance,
)


def guest(text: str, at: str) -> dict:
    return {"sender": "Renter", "message": text, "created_at": at}


def owner(text: str, at: str) -> dict:
    return {"sender": "Owner", "message": text, "created_at": at}


# The live thread that exposed the bug, reduced to its shape.
EARLY_CHECK_IN = guest(
    "Hi! Is early check-in possible? Also, does the place have a wine bottle opener?",
    "2026-09-01T09:00:00",
)

OWNER_ANSWERED = owner(
    "Early check-in depends on the previous checkout and cleaning -- I'll confirm "
    "closer to your stay. And yes, there's an opener in the kitchen drawer.",
    "2026-09-01T10:00:00",
)

THANK_YOU = guest("Thank you!", "2026-09-01T11:00:00")


# -- 1. answered question + "Thank you" -> NO_REPLY_NEEDED ----------------


def test_acknowledgement_after_an_answer_needs_no_reply():
    state = analyse_conversation([EARLY_CHECK_IN, OWNER_ANSWERED, THANK_YOU])

    assert state["suggested_outcome"] == "no_reply_needed"
    assert state["latest_guest_message_is_acknowledgement"] is True


def test_the_answered_question_is_marked_as_already_handled():
    state = analyse_conversation([EARLY_CHECK_IN, OWNER_ANSWERED, THANK_YOU])

    # The original two-part question sits in the answered bucket, so the model
    # is told not to answer it again rather than left to work it out.
    assert EARLY_CHECK_IN["message"] in state["answered_earlier_by_us"]
    assert EARLY_CHECK_IN["message"] not in state["unanswered_guest_messages"]


# -- 6. the specific regression: no re-answering the wine opener ----------


def test_a_resolved_topic_is_not_left_open_for_the_model_to_re_answer():
    state = analyse_conversation([EARLY_CHECK_IN, OWNER_ANSWERED, THANK_YOU])

    open_text = " ".join(state["unanswered_guest_messages"])

    assert "wine" not in open_text.lower()
    assert "early check-in" not in open_text.lower()

    # The only thing still open is the thank-you, and it asks for nothing.
    assert state["unanswered_guest_messages"] == ["Thank you!"]


# -- 2/7. acknowledgement carrying a new question -------------------------


def test_a_thank_you_with_a_new_question_still_needs_a_reply():
    follow_up = guest("Thank you. Also, what time is checkout?", "2026-09-01T11:00:00")

    state = analyse_conversation([EARLY_CHECK_IN, OWNER_ANSWERED, follow_up])

    assert state["suggested_outcome"] == "reply_needed"
    assert state["latest_guest_message_is_acknowledgement"] is False
    assert state["unanswered_guest_messages"] == [follow_up["message"]]


def test_only_the_new_question_is_open():
    follow_up = guest("Thanks! Is there parking?", "2026-09-01T11:00:00")

    state = analyse_conversation([EARLY_CHECK_IN, OWNER_ANSWERED, follow_up])

    assert state["unanswered_guest_messages"] == ["Thanks! Is there parking?"]
    assert "wine" not in " ".join(state["unanswered_guest_messages"]).lower()


# -- 3. unanswered question alongside an answered one ---------------------


def test_a_question_asked_after_our_reply_is_the_open_one():
    second_question = guest(
        "One more -- is there a coffee maker?", "2026-09-01T12:00:00"
    )

    state = analyse_conversation(
        [EARLY_CHECK_IN, OWNER_ANSWERED, THANK_YOU, second_question]
    )

    assert state["suggested_outcome"] == "reply_needed"
    assert state["unanswered_guest_messages"] == [
        "Thank you!",
        "One more -- is there a coffee maker?",
    ]
    assert EARLY_CHECK_IN["message"] in state["answered_earlier_by_us"]


# -- 4. conditional answer + acknowledgement ------------------------------


def test_a_conditional_answer_followed_by_thanks_is_not_repeated():
    asked = guest("Any word on early check-in?", "2026-09-02T09:00:00")
    conditional = owner(
        "We'll know tonight -- I'll message you.", "2026-09-02T10:00:00"
    )
    thanks = guest("Sounds good, thanks.", "2026-09-02T11:00:00")

    state = analyse_conversation([asked, conditional, thanks])

    # Unresolved, but not a reason to say the same thing again.
    assert state["suggested_outcome"] == "no_reply_needed"
    assert asked["message"] in state["answered_earlier_by_us"]


# -- 5. the guest asks again ----------------------------------------------


def test_a_repeated_unresolved_question_needs_a_reply():
    asked = guest("Any word on early check-in?", "2026-09-02T09:00:00")
    conditional = owner(
        "We'll know tonight -- I'll message you.", "2026-09-02T10:00:00"
    )
    again = guest(
        "Sorry to chase -- any update on the early check-in?", "2026-09-03T09:00:00"
    )

    state = analyse_conversation([asked, conditional, again])

    assert state["suggested_outcome"] == "reply_needed"
    assert state["unanswered_guest_messages"] == [again["message"]]


# -- acknowledgement detection --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Thank you!",
        "thanks",
        "Thanks so much!!",
        "Sounds good",
        "Okay",
        "Perfect",
        "Got it",
        "ok great thanks",
        "Thank you 🙏",
        "Much appreciated.",
    ],
)
def test_closings_are_recognised_as_acknowledgements(text):
    assert is_acknowledgement(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Thanks -- what time is checkout?",
        "Thank you, could you also confirm parking?",
        "ok but the heating is not working and it is very cold in here",
        "Thanks! One more thing, is there a coffee maker",
        "",
    ],
)
def test_a_message_carrying_a_request_is_not_an_acknowledgement(text):
    assert is_acknowledgement(text) is False


def test_a_question_mark_always_defeats_acknowledgement_detection():
    # Deliberately asymmetric: missing an acknowledgement costs one needless
    # reply, misreading a question as one costs an ignored guest.
    assert is_acknowledgement("Thanks?") is False


# -- edge cases ------------------------------------------------------------


def test_an_empty_conversation_is_not_reply_needed():
    state = analyse_conversation([])

    assert state["suggested_outcome"] == "already_replied"
    assert state["latest_guest_message"] is None
    assert state["awaiting_our_reply"] is False


def test_a_thread_where_we_spoke_last_has_nothing_open():
    state = analyse_conversation([EARLY_CHECK_IN, OWNER_ANSWERED])

    assert state["suggested_outcome"] == "already_replied"
    assert state["unanswered_guest_messages"] == []


def test_a_first_message_from_a_guest_needs_a_reply():
    state = analyse_conversation([EARLY_CHECK_IN])

    assert state["suggested_outcome"] == "reply_needed"
    assert state["unanswered_guest_messages"] == [EARLY_CHECK_IN["message"]]


def test_analysis_tolerates_malformed_rows():
    assert analyse_conversation(None)["message_count"] == 0
    assert analyse_conversation(["not a dict"])["message_count"] == 0


# -- guidance content ------------------------------------------------------


def test_guidance_carries_the_conversation_state():
    guidance = reply_guidance([EARLY_CHECK_IN, OWNER_ANSWERED, THANK_YOU])

    assert guidance["conversation_state"]["suggested_outcome"] == "no_reply_needed"


def test_guidance_explains_how_to_read_a_conversation():
    rules = " ".join(reply_guidance()["how_to_read_the_conversation"]).lower()

    assert "whole conversation in order" in rules
    assert "most recent message" in rules
    assert "already answered" in rules
    assert "only to what is still open" in rules


def test_guidance_defines_the_no_reply_outcome():
    guidance = reply_guidance()

    assert NO_REPLY_NEEDED == "NO_REPLY_NEEDED"
    assert NO_REPLY_NEEDED in guidance["no_reply_needed"]

    # And says when it is *not* correct, which is the half a model gets wrong.
    assert "new question" in guidance["no_reply_needed"]


def test_topic_rules_are_marked_conditional():
    # The bug in one sentence: the topic rules were being read as a checklist.
    conditional = reply_guidance()["topic_rules_are_conditional"].lower()

    assert "not a checklist" in conditional
    assert "did not raise" in conditional


# -- 9. concision ----------------------------------------------------------


def test_voice_asks_for_a_short_direct_reply():
    voice = reply_guidance()["voice"].lower()

    assert "one to three sentences" in voice
    assert "answer the question first" in voice
    assert "do not summarise the conversation" in voice
    assert "do not repeat the guest's question" in voice


def test_customer_service_boilerplate_is_named_and_banned():
    avoid = " ".join(reply_guidance()["avoid_phrases"]).lower()

    assert "thank you for reaching out" in avoid
    assert "we appreciate your inquiry" in avoid
    assert "do not hesitate" in avoid


# -- 8. no invented property facts ----------------------------------------


def test_amenities_may_not_be_answered_from_memory():
    topics = " ".join(reply_guidance()["do_not_answer_from_memory"]).lower()

    # The exact class of claim that produced "we should have a wine bottle
    # opener available".
    assert "amenity" in topics
    assert "present in a unit" in topics


def test_guidance_forbids_softening_a_guess_into_a_promise():
    never = reply_guidance()["never_invent"].lower()

    assert "should have" in never
    assert "authoritative tool result" in never
    assert "say you will check" in never


def test_guidance_is_json_serialisable_for_the_model():
    # It travels to the model as a tool result, so it has to survive json.dumps.
    json.dumps(reply_guidance([EARLY_CHECK_IN, OWNER_ANSWERED, THANK_YOU]))
