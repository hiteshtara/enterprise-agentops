"""Which guest questions AgentGuard answers, and which it routes to a person.

The bug these tests pin down: an ordinary question -- "any good restaurants
nearby?" -- was handed to the owner, because the only fallback rule said any
question the file does not cover gets a warm acknowledgement and a promise that
a person will follow up. That blanket made the owner the answering service for
questions a model answers perfectly well.

The fix is three layers, and each layer is tested separately because each one
has to hold on its own:

  1. A deterministic gate in Python decides whether an open message touches the
     property, the reservation, money, or a promise to the guest. The model is
     told the verdict; it does not compute it.
  2. The fallback flips. Not business-sensitive means answer it; the
     acknowledgement is reserved for business-sensitive questions that policy
     and approved knowledge cannot answer.
  3. The fabrication guard binds regardless of the routing verdict. It is the
     backstop for a marker this file failed to think of, which is exactly why
     it is asserted independently of layers 1 and 2.

Every guest message here is invented. None of it is real guest text.
"""

import pytest

from app.hospitality import (
    BUSINESS_MARKER_GROUPS,
    BUSINESS_MARKERS,
    analyse_conversation,
    business_categories,
    is_business_sensitive,
    reply_guidance,
)


def guest(text: str, at: str = "2026-09-01T09:00:00") -> dict:
    return {"sender": "Renter", "message": text, "created_at": at}


def owner(text: str, at: str = "2026-09-01T10:00:00") -> dict:
    return {"sender": "Owner", "message": text, "created_at": at}


def state_for(text: str) -> dict:
    return analyse_conversation([guest(text)])


def guidance_for(text: str) -> dict:
    return reply_guidance([guest(text)])


# -- layer 1: the gate is deterministic and answers with categories --------


GENERAL_QUESTIONS: tuple[str, ...] = (
    "Any good restaurants nearby?",
    "How do I get downtown from here?",
    "What should we see in Boston while we are there?",
    "Where is a grocery store near you?",
    # Matches no topic rule at all -- the case the old blanket fallback sent
    # straight to the owner.
    "Do you know if the museum is open on Mondays?",
)


@pytest.mark.parametrize("text", GENERAL_QUESTIONS)
def test_an_ordinary_question_is_not_business_sensitive(text):
    state = state_for(text)

    assert state["business_sensitive"] is False
    assert state["business_categories"] == []


@pytest.mark.parametrize("text", GENERAL_QUESTIONS)
def test_an_ordinary_question_does_not_escalate_to_the_owner(text):
    state = state_for(text)

    # Nothing about a restaurant is the owner's decision, and nothing about it
    # is a policy topic either.
    assert state["owner_approval_required"] is False
    assert state["late_checkout_requested"] is False
    assert state["early_check_in_requested"] is False
    assert state["stay_extension_requested"] is False

    assert guidance_for(text).get("owner_approval_required") is None


BUSINESS_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Does this apartment have a blender?", "amenity_present"),
    ("Can I check out at noon?", "late_checkout"),
    ("Can I get a refund?", "refunds"),
    ("Can we add another night to the booking?", "reservation_change"),
    (
        "The kitchen tap is leaking and there is water all over the floor.",
        "damage_maintenance_safety",
    ),
    ("The wifi keeps dropping and we cannot get online.", "internet"),
)


@pytest.mark.parametrize(("text", "category"), BUSINESS_QUESTIONS)
def test_a_business_question_is_flagged_with_its_category(text, category):
    state = state_for(text)

    assert state["business_sensitive"] is True
    assert category in state["business_categories"]


# -- layer 1: the thirteen categories the owner added ----------------------
#
# One invented example each, so the marker set is demonstrably wired rather
# than merely declared. A category that stops matching its own example is a
# category that quietly stopped protecting anything.

OWNER_ADDED_CATEGORIES: tuple[tuple[str, str], ...] = (
    (
        "cleaning",
        "Could someone come and clean the flat halfway through our stay?",
    ),
    ("noise", "The neighbours were very loud until two in the morning."),
    ("lost_and_found", "I think I left my sunglasses on the bedside table."),
    ("smoking", "Is it alright to smoke on the balcony?"),
    ("parties_events", "We would like to have a small birthday party there."),
    ("luggage_storage", "Can we leave our luggage with you afterwards?"),
    ("deliveries", "A parcel is arriving tomorrow, could you take it in?"),
    ("accessibility", "My mother uses a wheelchair -- is the entrance step-free?"),
    ("compensation", "After all that, could we have some compensation?"),
    ("internet", "The internet has been down since this morning."),
    ("utilities", "There is no hot water in the bathroom today."),
    ("security", "The front door was left unlocked all night and we are worried."),
    ("keys_lockouts", "We are locked out and the key will not turn."),
)


@pytest.mark.parametrize(("category", "text"), OWNER_ADDED_CATEGORIES)
def test_each_owner_added_category_matches_a_real_example(category, text):
    assert is_business_sensitive(text) is True
    assert category in business_categories(text)


def test_every_declared_category_is_reachable_from_the_flat_marker_tuple():
    # The flat tuple is what `contains_marker` consumes; the groups are what
    # names a match. They must be built from each other, not maintained twice.
    flattened = tuple(
        marker for _category, markers in BUSINESS_MARKER_GROUPS for marker in markers
    )

    assert flattened == BUSINESS_MARKERS


def test_markers_are_lowercase_so_normalised_text_can_match_them():
    assert all(marker == marker.lower() for marker in BUSINESS_MARKERS)


def test_a_marker_inside_a_longer_word_does_not_match():
    # `contains_marker` matches whole words. "keys" must not fire on "monkeys",
    # and "neighbour" must not fire on "neighbourhood" -- neighbourhoods are
    # exactly the general question this change exists to allow.
    assert business_categories("Are there monkeys at the zoo?") == ()
    assert business_categories("Which neighbourhood should we stay in?") == ()


# -- layer 1: "how much" only means money in the pricing group -------------
#
# The bare marker "how much" also matched measurement questions -- "how much
# time does it take to get downtown?" is a transit question, not a pricing
# one, and escalated anyway. Only money-shaped "how much" phrasings should
# fire the pricing category.

HOW_MUCH_TRAVEL_QUESTIONS: tuple[str, ...] = (
    "How much time does it take to get downtown?",
    "How much walking is it to the river?",
    "How far is the museum?",
)


@pytest.mark.parametrize("text", HOW_MUCH_TRAVEL_QUESTIONS)
def test_how_much_measurement_questions_are_not_business_sensitive(text):
    assert is_business_sensitive(text) is False
    assert business_categories(text) == ()


HOW_MUCH_PRICING_QUESTIONS: tuple[str, ...] = (
    "How much is the cleaning fee?",
    "How much does it cost to park?",
    "What's the price for an extra guest?",
    "Is there a deposit?",
)


@pytest.mark.parametrize("text", HOW_MUCH_PRICING_QUESTIONS)
def test_how_much_pricing_questions_still_match_the_pricing_category(text):
    assert is_business_sensitive(text) is True
    assert "pricing" in business_categories(text)


def test_how_much_charge_question_still_matches_pricing_among_its_categories():
    # This one also touches late checkout -- that overlap is fine, pricing
    # must simply still be one of the categories returned.
    text = "How much do you charge for late checkout?"

    assert is_business_sensitive(text) is True
    assert "pricing" in business_categories(text)


# -- layer 1: the gate reads only what is still open -----------------------


def test_an_answered_business_question_does_not_re_fire():
    thread = [
        guest("Can I get a refund on the cleaning fee?"),
        owner("I've refunded that today -- it should land in a few days."),
        guest("Thank you!", "2026-09-01T11:00:00"),
    ]

    state = analyse_conversation(thread)

    # Same list that stops late checkout and stay extension re-firing: only the
    # guest messages that arrived after our last reply can still be open.
    assert state["business_sensitive"] is False
    assert state["business_categories"] == []


def test_a_new_business_question_after_our_reply_does_fire():
    thread = [
        guest("Any good restaurants nearby?"),
        owner("Plenty -- the street behind you has several places to eat."),
        guest("Great. Can we check out at noon?", "2026-09-01T11:00:00"),
    ]

    state = analyse_conversation(thread)

    assert state["business_sensitive"] is True
    assert "late_checkout" in state["business_categories"]


def test_an_empty_conversation_is_not_business_sensitive():
    state = analyse_conversation([])

    assert state["business_sensitive"] is False
    assert state["business_categories"] == []


# -- layer 2: the fallback is flipped -------------------------------------


def test_the_fallback_rule_now_answers_ordinary_questions():
    rules = {rule["topic"]: rule["guidance"] for rule in reply_guidance()["rules"]}

    fallback = rules["general_acknowledgement"].lower()

    # It still says what to do when we genuinely cannot answer...
    assert "a person will follow up" in fallback

    # ...but that is no longer the answer to every unruled question.
    assert "general knowledge" in fallback
    assert "restaurant" in fallback


def test_general_questions_are_no_longer_banned_as_a_category():
    topics = " ".join(reply_guidance()["do_not_answer_from_memory"]).lower()

    # The entry is about endorsements and invented specifics, not about local
    # questions existing.
    assert "endorsement" in topics
    assert "local recommendations presented as endorsements" not in topics


@pytest.mark.parametrize("text", GENERAL_QUESTIONS)
def test_an_ordinary_question_gets_permission_to_be_answered(text):
    guidance = guidance_for(text)

    assert guidance["business_sensitivity"]["business_sensitive"] is False

    how = guidance["business_sensitivity"]["how_to_answer"].lower()

    assert "general knowledge" in how
    assert "answer" in how


@pytest.mark.parametrize(("text", "_category"), BUSINESS_QUESTIONS)
def test_a_business_question_is_routed_through_policy(text, _category):
    guidance = guidance_for(text)

    assert guidance["business_sensitivity"]["business_sensitive"] is True

    how = guidance["business_sensitivity"]["how_to_answer"].lower()

    assert "do not answer it from general knowledge" in how


def test_general_guidance_is_attached_conditionally():
    # Topic guidance says how to answer a topic *if it is raised*. A thread with
    # nothing open must not carry an invitation to talk about restaurants.
    settled = reply_guidance(
        [
            guest("Any good restaurants nearby?"),
            owner("Plenty of places on the street behind you."),
        ]
    )

    assert "business_sensitivity" not in settled
    assert "general_question_policy" not in settled

    closing = reply_guidance(
        [
            guest("Any good restaurants nearby?"),
            owner("Plenty of places on the street behind you."),
            guest("Perfect, thanks!", "2026-09-01T11:00:00"),
        ]
    )

    assert "business_sensitivity" not in closing


def test_general_guidance_never_asks_the_model_to_volunteer_local_information():
    policy = reply_guidance(
        [guest("Any good restaurants nearby?")],
    )["general_question_policy"].lower()

    assert "did not ask" in policy or "did not raise" in policy

    conditional = reply_guidance()["topic_rules_are_conditional"].lower()

    assert "not a checklist" in conditional


# -- mixed messages --------------------------------------------------------


MIXED = "Any good restaurants nearby, and can we check out at noon?"


def test_a_mixed_message_is_business_sensitive_because_of_its_business_half():
    state = state_for(MIXED)

    assert state["business_sensitive"] is True
    assert "late_checkout" in state["business_categories"]


def test_a_mixed_message_still_gets_the_late_checkout_policy():
    state = state_for(MIXED)

    # Noon is past the 11:00 AM ceiling, so this stays the owner's decision --
    # exactly as it did before routing existed.
    assert state["late_checkout_requested"] is True
    assert state["late_checkout_beyond_policy"] is True
    assert state["owner_approval_required"] is True


def test_a_mixed_message_may_still_have_its_general_half_answered():
    how = guidance_for(MIXED)["business_sensitivity"]["how_to_answer"].lower()

    assert "do not answer it from general knowledge" in how

    # ...and the other half is explicitly not collateral damage.
    assert "same message also asks something ordinary" in how
    assert "not general because part of it is" in how


# -- layer 3: the fabrication guard ---------------------------------------


EXACT_FACT_CLAIMS: tuple[str, ...] = (
    "opening hour",
    "rating",
    "travel time",
    "walking distance",
    "price",
    "transit",
)


@pytest.mark.parametrize("claim", EXACT_FACT_CLAIMS)
def test_the_guard_names_every_exact_fact_class(claim):
    never = reply_guidance()["never_invent"].lower()

    assert claim in never


def test_the_guard_keeps_the_original_property_fact_rule():
    # Extended, not replaced. The wine-opener regression is still covered.
    never = reply_guidance()["never_invent"].lower()

    assert "should have" in never
    assert "authoritative tool result" in never
    assert "say you will check" in never


def test_the_guard_shows_what_is_allowed_and_what_is_not():
    never = reply_guidance()["never_invent"].lower()

    assert "several restaurants and cafes in the neighbourhood" in never
    assert "two minutes from your front door" in never
    assert "4-minute walk" in never


@pytest.mark.parametrize("text", GENERAL_QUESTIONS)
def test_the_guard_binds_even_when_routing_says_the_question_is_general(text):
    # The independent backstop for a marker nobody thought of. If it only
    # applied to business-sensitive questions, one missed marker would license
    # an invented walking distance.
    guidance = guidance_for(text)

    assert guidance["business_sensitivity"]["business_sensitive"] is False

    never = guidance["never_invent"].lower()

    assert "walking distance" in never
    assert "opening hour" in never

    how = guidance["business_sensitivity"]["how_to_answer"].lower()

    assert "never_invent" in how


# -- nothing existing was weakened ----------------------------------------


def test_the_checkout_ceiling_is_unchanged_by_routing():
    policy = reply_guidance([guest("Can we check out at noon?")])

    assert "10:00 AM" in policy["late_checkout_policy"]
    assert "11:00 AM" in policy["late_checkout_policy"]
    assert policy.get("owner_approval_required")


def test_the_escalation_rule_is_still_present_and_topic_gated():
    escalation = reply_guidance()["escalation"].lower()

    assert "do_not_answer_from_memory" in escalation
    assert "approved_knowledge" in escalation
    assert "confident guess" in escalation
