"""Knowledge lifecycle, safety filtering, distillation and conflict semantics.

Every guest and owner message here is invented. Real guest text never appears in
source, fixtures, docs or comments.

No test reaches Lodgify or OpenAI: distillation is driven through a fake model.
"""

import json

import pytest

from app.distill_knowledge import (
    build_prompt,
    distil,
    group_examples,
    parse_candidates,
)
from app.historical_replies import HistoricalReplyStore, extract_exchanges
from app.hospitality import (
    CONVERSATION_EXCEPTION_LABEL,
    conversation_exceptions,
    looks_like_commitment,
    reply_guidance,
)
from app.knowledge import (
    KnowledgeSource,
    KnowledgeStatus,
    KnowledgeStore,
    knowledge_ref_for,
)
from app.knowledge_safety import check_candidate
from app.knowledge_topics import GUEST_FACING


@pytest.fixture
def knowledge(database):
    return KnowledgeStore(database=database)


def guest(text, at="2026-03-01T10:00:00"):
    return {"sender": "Renter", "message": text, "created_at": at, "subject": "Q"}


def owner(text, at="2026-03-01T11:00:00"):
    return {"sender": "Owner", "message": text, "created_at": at, "subject": "A"}


class FakeModel:
    """Returns scripted JSON. Never calls OpenAI."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def generate(self, message: str) -> str:
        self.prompts.append(message)

        return self.answers.pop(0) if self.answers else '{"candidates": []}'

    def generate_with_tools(self, messages, tools):  # pragma: no cover
        raise NotImplementedError


def candidate_json(title, content, scope="property", reason="Seen repeatedly."):
    return json.dumps(
        {
            "candidates": [
                {
                    "title": title,
                    "content": content,
                    "reason": reason,
                    "scope": scope,
                }
            ]
        }
    )


# -- lifecycle -------------------------------------------------------------


def test_a_candidate_starts_proposed(knowledge):
    item, created = knowledge.propose(
        property_slug="renovated-2nd-floor-home",
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    assert created is True
    assert item.status == KnowledgeStatus.PROPOSED.value
    assert item.source_type == KnowledgeSource.HISTORICAL_DISTILLATION.value
    assert item.decided_by_user_id is None


def test_proposing_never_approves(knowledge):
    knowledge.propose(
        property_slug=None,
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    assert knowledge.approved_for("renovated-2nd-floor-home") == []


def test_only_an_explicit_decision_approves(knowledge):
    item, _ = knowledge.propose(
        property_slug="renovated-2nd-floor-home",
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    approved = knowledge.decide(
        item.knowledge_ref,
        KnowledgeStatus.APPROVED,
        actor_user_id="user-admin-1",
    )

    assert approved.status == KnowledgeStatus.APPROVED.value
    assert approved.decided_by_user_id == "user-admin-1"
    assert approved.decided_at is not None


def test_a_decision_always_records_an_actor(knowledge):
    item, _ = knowledge.propose(
        property_slug=None,
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    rejected = knowledge.decide(
        item.knowledge_ref,
        KnowledgeStatus.REJECTED,
        actor_user_id="user-admin-2",
    )

    assert rejected.status == KnowledgeStatus.REJECTED.value
    assert rejected.decided_by_user_id == "user-admin-2"


def test_proposed_is_not_a_decision(knowledge):
    item, _ = knowledge.propose(
        property_slug=None,
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    with pytest.raises(ValueError):
        knowledge.decide(
            item.knowledge_ref,
            KnowledgeStatus.PROPOSED,
            actor_user_id="user-admin-1",
        )


def test_editing_does_not_approve(knowledge):
    item, _ = knowledge.propose(
        property_slug="renovated-2nd-floor-home",
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    edited = knowledge.update(
        item.knowledge_ref,
        actor_user_id="user-admin-1",
        content="Parking is shared and is not allocated to a particular unit.",
    )

    assert edited.status == KnowledgeStatus.PROPOSED.value
    assert knowledge.approved_for("renovated-2nd-floor-home") == []


def test_widening_scope_needs_an_explicit_flag(knowledge):
    item, _ = knowledge.propose(
        property_slug="renovated-2nd-floor-home",
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    unchanged = knowledge.update(
        item.knowledge_ref, actor_user_id="user-admin-1", property_slug=None
    )

    assert unchanged.property_slug == "renovated-2nd-floor-home"

    widened = knowledge.update(
        item.knowledge_ref, actor_user_id="user-admin-1", scope_to_global=True
    )

    assert widened.property_slug is None
    assert widened.scope == "global"


def test_re_proposing_refreshes_rather_than_duplicating(knowledge):
    for _ in range(2):
        knowledge.propose(
            property_slug="renovated-2nd-floor-home",
            topic="parking",
            title="Shared parking",
            content="Parking is shared between guests and is not reserved.",
            evidence_refs=("a", "b", "c"),
        )

    assert len(knowledge.list_knowledge()) == 1


def test_a_rejected_candidate_is_not_requeued(knowledge):
    item, _ = knowledge.propose(
        property_slug=None,
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    knowledge.decide(
        item.knowledge_ref, KnowledgeStatus.REJECTED, actor_user_id="user-admin-1"
    )

    again, created = knowledge.propose(
        property_slug=None,
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
    )

    # A rejection is an answer. Re-proposing it would argue with the reviewer.
    assert created is False
    assert again.status == KnowledgeStatus.REJECTED.value


def test_knowledge_ref_is_stable_and_scope_sensitive():
    first = knowledge_ref_for("a", "parking", "Shared parking")

    assert first == knowledge_ref_for("a", "parking", "shared parking  ")
    assert first != knowledge_ref_for(None, "parking", "Shared parking")
    assert first != knowledge_ref_for("b", "parking", "Shared parking")


# -- scope -----------------------------------------------------------------


def test_approved_reads_pick_up_property_and_global_rules(knowledge):
    for slug, title in (
        ("renovated-2nd-floor-home", "Shared parking here"),
        (None, "Early check-in depends on cleaning"),
        ("boston-condo-second-floor", "Different property rule"),
    ):
        item, _ = knowledge.propose(
            property_slug=slug,
            topic="parking",
            title=title,
            content="Some durable operational rule about this topic.",
            audience=GUEST_FACING,
        )

        knowledge.decide(
            item.knowledge_ref, KnowledgeStatus.APPROVED, actor_user_id="user-admin-1"
        )

    titles = [item.title for item in knowledge.approved_for("renovated-2nd-floor-home")]

    assert "Shared parking here" in titles
    assert "Early check-in depends on cleaning" in titles
    assert "Different property rule" not in titles


def test_property_specific_rules_come_before_global_ones(knowledge):
    for slug, title in ((None, "Global rule"), ("property-a", "Property rule")):
        item, _ = knowledge.propose(
            property_slug=slug,
            topic="parking",
            title=title,
            content="Some durable operational rule about this topic.",
            audience=GUEST_FACING,
        )
        knowledge.decide(
            item.knowledge_ref, KnowledgeStatus.APPROVED, actor_user_id="user-admin-1"
        )

    assert knowledge.approved_for("property-a")[0].title == "Property rule"


# -- safety filtering ------------------------------------------------------


GOOD = "Parking is shared between guests and is not allocated to a unit."


def test_a_clean_candidate_is_accepted():
    verdict = check_candidate(
        title="Shared parking",
        content=GOOD,
        property_slug="property-a",
        evidence_count=6,
        evidence_property_count=1,
    )

    assert verdict.accepted is True
    assert verdict.reasons == ()


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("Parking costs $100 per stay for guests here.", "contains_price"),
        ("Parking costs 100 USD per stay for guests.", "contains_price"),
        (
            "The door code for the parking gate is given on arrival.",
            "contains_access_code",
        ),
        ("Use code 48213 to open the parking gate on arrival.", "contains_access_code"),
        ("Parking was suspended from 2026-03-01 for works.", "contains_specific_date"),
        ("Parking is arranged for your booking as we agreed.", "booking_specific"),
        ("Parking is always available for every guest here.", "overpromises"),
        ("Parking is guaranteed to every guest who asks.", "overpromises"),
        (
            "Ask [redacted] about the parking arrangements here.",
            "contains_identity_or_link",
        ),
        ("Short.", "content_too_short"),
    ],
)
def test_unsafe_candidates_are_rejected(content, reason):
    verdict = check_candidate(
        title="Parking",
        content=content,
        property_slug="property-a",
        evidence_count=6,
        evidence_property_count=1,
    )

    assert verdict.accepted is False
    assert reason in verdict.reasons


def test_a_single_example_is_not_enough_evidence():
    verdict = check_candidate(
        title="Shared parking",
        content=GOOD,
        property_slug="property-a",
        evidence_count=1,
        evidence_property_count=1,
    )

    assert verdict.accepted is False
    assert "insufficient_evidence" in verdict.reasons


def test_global_scope_on_one_propertys_evidence_is_narrowed_not_rejected():
    verdict = check_candidate(
        title="Shared parking",
        content=GOOD,
        property_slug=None,
        evidence_count=8,
        evidence_property_count=1,
    )

    # A good rule with the wrong scope. Narrow it, do not throw it away.
    assert verdict.accepted is True
    assert verdict.forced_property_scope is True


def test_global_scope_survives_when_several_properties_agree():
    verdict = check_candidate(
        title="Early check-in depends on cleaning",
        content="Early check-in depends on the previous checkout and on cleaning.",
        property_slug=None,
        evidence_count=20,
        evidence_property_count=4,
    )

    assert verdict.accepted is True
    assert verdict.forced_property_scope is False


# -- distillation ----------------------------------------------------------


@pytest.fixture
def archive(database):
    store = HistoricalReplyStore(database=database)

    for index in range(4):
        store.upsert(
            extract_exchanges(
                [
                    guest("Where do we park?", f"2026-03-0{index + 1}T09:00:00"),
                    owner("Parking is shared out front and is not reserved."),
                ],
                property_slug="renovated-2nd-floor-home",
            )
        )

    return store


def test_distillation_stores_candidates_as_proposed(archive, knowledge):
    model = FakeModel([candidate_json("Shared parking", GOOD)])

    report = distil(archive, knowledge, model)

    assert report.candidates_stored == 1

    item = knowledge.list_knowledge()[0]

    assert item.status == KnowledgeStatus.PROPOSED.value
    assert knowledge.approved_for("renovated-2nd-floor-home") == []


def test_distillation_never_approves_anything(archive, knowledge):
    distil(archive, knowledge, FakeModel([candidate_json("Shared parking", GOOD)]))

    assert knowledge.counts()[KnowledgeStatus.APPROVED.value] == 0


def test_distillation_records_evidence_not_guest_text(archive, knowledge):
    distil(archive, knowledge, FakeModel([candidate_json("Shared parking", GOOD)]))

    item = knowledge.list_knowledge()[0]

    assert item.evidence_count >= 2
    assert item.first_observed_at is not None

    body = json.dumps(item.to_dict())

    assert "Where do we park" not in body


def test_the_prompt_carries_owner_replies_only(archive, knowledge):
    model = FakeModel([candidate_json("Shared parking", GOOD)])

    distil(archive, knowledge, model)

    prompt = model.prompts[0]

    assert "Parking is shared out front" in prompt
    # The guest's own words are never sent.
    assert "Where do we park" not in prompt


def test_unsafe_candidates_are_filtered_before_storage(archive, knowledge):
    model = FakeModel([candidate_json("Parking", "Parking costs $100 per stay here.")])

    report = distil(archive, knowledge, model)

    assert report.candidates_rejected == 1
    assert report.candidates_stored == 0
    assert "contains_price" in report.rejection_reasons
    assert knowledge.list_knowledge() == []


def test_a_model_failure_does_not_abort_the_run(archive, knowledge):
    class Broken(FakeModel):
        def generate(self, message):
            raise RuntimeError("provider down")

    report = distil(archive, knowledge, Broken([]))

    assert report.model_failures >= 1
    assert report.candidates_stored == 0


def test_malformed_model_output_yields_no_candidates(archive, knowledge):
    report = distil(archive, knowledge, FakeModel(["not json at all"]))

    assert report.candidates_returned == 0
    assert report.candidates_stored == 0


def test_small_groups_are_skipped(database, knowledge):
    store = HistoricalReplyStore(database=database)

    store.upsert(
        extract_exchanges(
            [guest("Where do we park?"), owner("Shared out front.")],
            property_slug="renovated-2nd-floor-home",
        )
    )

    model = FakeModel([candidate_json("Shared parking", GOOD)])

    report = distil(store, knowledge, model)

    assert report.groups_analysed == 0
    assert report.groups_skipped_small >= 1
    assert model.prompts == []


def test_grouping_is_by_property_and_topic():
    groups = group_examples(
        [
            {"property_slug": "a", "topics": ["parking"], "owner_text": "x"},
            {"property_slug": "a", "topics": ["parking", "wifi"], "owner_text": "y"},
            {"property_slug": "b", "topics": ["parking"], "owner_text": "z"},
        ]
    )

    assert len(groups[("a", "parking")]) == 2
    assert len(groups[("a", "wifi")]) == 1
    assert len(groups[("b", "parking")]) == 1


def test_candidate_parsing_tolerates_fences_and_prose():
    fenced = '```json\n{"candidates": [{"title": "t", "content": "c"}]}\n```'

    assert len(parse_candidates(fenced)) == 1

    chatty = (
        'Here you go: {"candidates": [{"title": "t", "content": "c"}]} Hope that helps.'
    )

    assert len(parse_candidates(chatty)) == 1

    assert parse_candidates("") == []
    assert parse_candidates("nonsense") == []


def test_the_prompt_forbids_prices_codes_and_dates():
    prompt = build_prompt("property-a", "parking", ["Parking is shared."])

    lowered = prompt.lower()

    assert "do not include any price" in lowered
    assert "door code" in lowered
    assert "do not include a specific date" in lowered
    assert "one particular guest" in lowered
    assert "one-off exception" in lowered


# -- conflict semantics ----------------------------------------------------


APPROVED_EARLY_CHECK_IN = {
    "topic": "early_check_in",
    "scope": "global",
    "title": "Early check-in is not guaranteed",
    "content": "Early check-in depends on the previous checkout and on cleaning.",
}


def test_a_commitment_in_this_thread_creates_an_exception():
    exceptions = conversation_exceptions(
        [
            guest("Can we check in early?"),
            owner("Yes, you can check in at noon."),
        ],
        [APPROVED_EARLY_CHECK_IN],
    )

    assert len(exceptions) == 1

    exception = exceptions[0]

    assert exception["marker"] == CONVERSATION_EXCEPTION_LABEL
    assert exception["topic"] == "early_check_in"
    assert exception["approved_rule"] == APPROVED_EARLY_CHECK_IN["content"]
    assert "check in at noon" in exception["commitment_made_in_this_thread"]


def test_the_exception_says_honour_it_and_do_not_generalise():
    exception = conversation_exceptions(
        [guest("Can we check in early?"), owner("Yes, you can check in at noon.")],
        [APPROVED_EARLY_CHECK_IN],
    )[0]

    instruction = exception["instruction"].lower()

    assert "honour the commitment" in instruction
    assert "do not treat this as a new rule" in instruction
    assert "this guest and this conversation only" in instruction


def test_no_commitment_means_no_exception():
    assert (
        conversation_exceptions(
            [
                guest("Can we check in early?"),
                owner("It depends on the previous checkout, I'll confirm."),
            ],
            [APPROVED_EARLY_CHECK_IN],
        )
        == []
    )


def test_a_commitment_on_another_topic_does_not_create_an_exception():
    assert (
        conversation_exceptions(
            [
                guest("Where do we park?"),
                owner("Yes, you can use the driveway."),
            ],
            [APPROVED_EARLY_CHECK_IN],
        )
        == []
    )


def test_a_guest_saying_yes_is_not_a_commitment():
    # Only the host can commit the business.
    assert (
        conversation_exceptions(
            [guest("Yes, you can expect us at noon for early check-in.")],
            [APPROVED_EARLY_CHECK_IN],
        )
        == []
    )


@pytest.mark.parametrize(
    "text",
    [
        "Yes, you can check in at noon.",
        "That works, we can do noon.",
        "No problem, go ahead.",
        "Confirmed for the earlier time.",
    ],
)
def test_commitment_language_is_recognised(text):
    assert looks_like_commitment(text) is True


def test_a_conditional_answer_is_not_a_commitment():
    assert looks_like_commitment("It depends on cleaning; I'll let you know.") is False


def test_guidance_carries_knowledge_and_exceptions():
    guidance = reply_guidance(
        [guest("Can we check in early?"), owner("Yes, you can check in at noon.")],
        [APPROVED_EARLY_CHECK_IN],
    )

    assert guidance["approved_knowledge"] == [APPROVED_EARLY_CHECK_IN]
    assert guidance["current_conversation_exceptions"]
    assert "authoritative" in guidance["approved_knowledge_authority"].lower()


def test_approved_knowledge_outranks_history_in_the_stated_order():
    order = reply_guidance()["authority_order"]

    approved = next(i for i, line in enumerate(order) if "OWNER-APPROVED" in line)
    commitment = next(i for i, line in enumerate(order) if "COMMITMENT" in line)
    history = next(i for i, line in enumerate(order) if "HISTORICAL" in line)

    assert commitment < approved < history


# -- review API ------------------------------------------------------------


@pytest.fixture
def knowledge_api(api):
    """The reloaded app with one PROPOSED candidate waiting."""
    from app.knowledge import KnowledgeStore as Store

    store = Store(database=api.module.database)

    item, _ = store.propose(
        property_slug="renovated-2nd-floor-home",
        topic="parking",
        title="Shared parking",
        content="Parking is shared between guests and is not reserved.",
        audience=GUEST_FACING,
        evidence_refs=("a", "b", "c"),
        evidence_property_count=1,
    )

    api.knowledge_ref = item.knowledge_ref
    api.store = store

    return api


def test_anyone_signed_in_can_read_the_queue(knowledge_api):
    response = knowledge_api.client("VIEWER").get("/knowledge")

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["counts"]["PROPOSED"] == 1
    assert payload["items"][0]["status"] == "PROPOSED"
    assert payload["items"][0]["scope"] == "renovated-2nd-floor-home"


def test_reading_the_queue_requires_authentication(knowledge_api):
    assert knowledge_api.anonymous().get("/knowledge").status_code == 401


def test_only_an_admin_may_approve(knowledge_api):
    for role in ("VIEWER", "OPERATOR", "APPROVER"):
        response = knowledge_api.client(role).post(
            f"/knowledge/{knowledge_api.knowledge_ref}/approve"
        )

        assert response.status_code == 403, role

    # Still proposed: no non-admin decision took effect.
    assert knowledge_api.store.get(knowledge_api.knowledge_ref).status == "PROPOSED"


def test_an_admin_approval_is_recorded_with_the_actor(knowledge_api):
    client = knowledge_api.client("ADMIN")

    response = client.post(f"/knowledge/{knowledge_api.knowledge_ref}/approve")

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["status"] == "APPROVED"
    assert payload["decided_by_user_id"]
    assert payload["decided_at"]

    events = client.get("/audit/events?event_type=KNOWLEDGE_APPROVED").json()

    assert len(events) == 1
    assert events[0]["details"]["title"] == "Shared parking"
    assert events[0]["actor_user_id"]


def test_rejection_is_recorded_too(knowledge_api):
    client = knowledge_api.client("ADMIN")

    assert (
        client.post(f"/knowledge/{knowledge_api.knowledge_ref}/reject").status_code
        == 200
    )

    assert client.get("/audit/events?event_type=KNOWLEDGE_REJECTED").json()


def test_an_unknown_decision_is_not_invented(knowledge_api):
    response = knowledge_api.client("ADMIN").post(
        f"/knowledge/{knowledge_api.knowledge_ref}/obliterate"
    )

    assert response.status_code == 404
    assert knowledge_api.store.get(knowledge_api.knowledge_ref).status == "PROPOSED"


def test_editing_requires_admin_and_does_not_approve(knowledge_api):
    assert (
        knowledge_api.client("APPROVER")
        .patch(
            f"/knowledge/{knowledge_api.knowledge_ref}",
            json={"content": "Parking is shared and unallocated."},
        )
        .status_code
        == 403
    )

    response = knowledge_api.client("ADMIN").patch(
        f"/knowledge/{knowledge_api.knowledge_ref}",
        json={"content": "Parking is shared and unallocated."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PROPOSED"
    assert response.json()["content"] == "Parking is shared and unallocated."


def test_an_unreviewed_candidate_never_reaches_drafting(knowledge_api):
    store = knowledge_api.store

    assert store.approved_for("renovated-2nd-floor-home") == []

    knowledge_api.client("ADMIN").post(
        f"/knowledge/{knowledge_api.knowledge_ref}/approve"
    )

    approved = store.approved_for("renovated-2nd-floor-home")

    assert len(approved) == 1
    assert approved[0].title == "Shared parking"


def test_unknown_reference_is_404(knowledge_api):
    assert knowledge_api.client("ADMIN").get("/knowledge/kn-nope").status_code == 404
    assert (
        knowledge_api.client("ADMIN").post("/knowledge/kn-nope/approve").status_code
        == 404
    )


# ==========================================================================
# V1.1: numeric safety, topic re-derivation, global promotion, consolidation
# ==========================================================================

from app.knowledge_consolidation import (
    MIN_PROPERTIES_FOR_GLOBAL,
    Candidate,
    consolidate,
    may_be_global,
    promote_global,
)
from app.knowledge_safety import (
    REJECT_SENSITIVE,
    REVIEW_NUMERIC_FACT,
    SAFE,
    numeric_signals,
)
from app.knowledge_topics import (
    INTERNAL_OPERATION,
    classify_audience,
    derive_topic,
)


def check(content, title="Rule", slug="property-a", count=6, properties=1):
    return check_candidate(
        title=title,
        content=content,
        property_slug=slug,
        evidence_count=count,
        evidence_property_count=properties,
    )


# -- 1. stale numeric facts ------------------------------------------------


@pytest.mark.parametrize(
    ("content", "signal"),
    [
        (
            "We can get guests in as early as 10 am when there is no checkout.",
            "clock_time",
        ),
        (
            "Early check-in is usually possible around 2 pm if the unit is ready.",
            "clock_time",
        ),
        ("Check-in opens at 16:00 once the unit has been cleaned.", "clock_time"),
        ("The nearest train station is about 1.5 miles from the house.", "distance"),
        ("Buses run one block from the front of the house.", "distance"),
        ("The airport is about a 20 minute drive depending on traffic.", "duration"),
        ("A 15 minute walk takes guests to the shops nearby.", "duration"),
        ("The driveway can fit two cars when parking is shared.", "capacity"),
        ("There are 3 parking spaces available for guests here.", "capacity"),
        ("A 10 percent discount applies to the room rate only.", "percentage"),
    ],
)
def test_operational_numbers_are_flagged_for_review_not_rejected(content, signal):
    verdict = check(content)

    # Kept -- these are useful. Flagged -- nobody has confirmed the number.
    assert verdict.accepted is True
    assert verdict.status == REVIEW_NUMERIC_FACT
    assert signal in verdict.numeric_signals
    assert f"numeric:{signal}" in verdict.reasons


@pytest.mark.parametrize(
    "content",
    [
        "Parking is shared between guests and is not allocated to a unit.",
        "The property does not accept one-night bookings from guests.",
        "Refunds are processed back through the guest's own payment provider.",
        "Early check-in depends on the previous checkout and on cleaning.",
    ],
)
def test_rules_without_operational_numbers_are_safe(content):
    verdict = check(content)

    assert verdict.accepted is True
    assert verdict.status == SAFE
    assert verdict.numeric_signals == ()


def test_two_night_bookings_is_a_policy_not_a_capacity():
    # "two-night bookings" describes a rule, not something the property has.
    assert numeric_signals("Two-night bookings may be accepted here.") == ()


def test_prices_are_still_rejected_outright():
    verdict = check("Parking costs $100 per stay for guests staying here.")

    assert verdict.accepted is False
    assert verdict.status == REJECT_SENSITIVE
    assert "contains_price" in verdict.reasons


def test_access_codes_are_still_rejected_outright():
    verdict = check("The door code is given to guests on the day of arrival.")

    assert verdict.accepted is False
    assert verdict.status == REJECT_SENSITIVE


def test_the_three_statuses_are_distinct():
    assert len({SAFE, REVIEW_NUMERIC_FACT, REJECT_SENSITIVE}) == 3


# -- 2. topic re-derivation ------------------------------------------------


@pytest.mark.parametrize(
    ("title", "content", "expected"),
    [
        # The live regression: an early check-in rule filed under `amenities`.
        (
            "Early check-in depends on the prior checkout schedule",
            "We can usually get guests in early when there is no checkout that day.",
            "early_check_in",
        ),
        # The live regression: refunds filed under `availability`.
        (
            "Refunds are handled according to the booking policy",
            "Where a stay is called off, we return money once it becomes due.",
            "refund",
        ),
        # The live regression: a parking rule filed under `location`.
        (
            "Parking is shared and driveway use is limited",
            "Parking is typically shared between the two units at this property.",
            "parking",
        ),
        (
            "Short stays are restricted",
            "The property does not accept one-night bookings from guests.",
            "minimum_stay",
        ),
        (
            "Transit and airport access",
            "The home is a short drive from the airport and near a bus route.",
            "location",
        ),
    ],
)
def test_topic_is_derived_from_the_rule_not_the_question(title, content, expected):
    assert derive_topic(title, content, fallback="amenities") == expected


def test_the_group_topic_is_only_a_fallback():
    assert derive_topic("Something", "Nothing recognisable here.", fallback="wifi") == (
        "wifi"
    )
    assert derive_topic("Something", "Nothing recognisable here.") == "general"


def test_early_check_in_beats_the_general_check_in_bucket():
    assert (
        derive_topic(
            "Check-in timing",
            "Early check-in may be possible when there is no checkout that day.",
        )
        == "early_check_in"
    )


# -- 5. audience -----------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "Parking is shared between guests and is not allocated to a unit.",
        "The property does not accept one-night bookings from guests.",
        "Early check-in depends on the previous checkout and on cleaning.",
        "The home is served by a nearby bus route into the city.",
    ],
)
def test_guest_facing_rules_are_classified_as_such(content):
    assert classify_audience("Rule", content) == GUEST_FACING


@pytest.mark.parametrize(
    "content",
    [
        "Staff should treat the internal booking record as the source of truth.",
        "Until the cancellation shows in the platform, treat the booking as active.",
        "If a guest wants another card, the payment can be cancelled and re-run.",
        "The owner may handle the adjustment manually and reconcile payment later.",
    ],
)
def test_internal_procedure_is_classified_as_internal(content):
    assert classify_audience("Rule", content) == INTERNAL_OPERATION


def test_an_unrecognisable_rule_defaults_to_internal():
    # Conservative: withholding a rule costs a draft that says "I'll check";
    # volunteering internal procedure to a guest is a different kind of error.
    assert classify_audience("Rule", "Zzz qqq wxy.") == INTERNAL_OPERATION


def test_internal_knowledge_is_excluded_from_guest_drafting(knowledge):
    for title, audience in (
        ("Shared parking", GUEST_FACING),
        ("Internal booking record", INTERNAL_OPERATION),
    ):
        item, _ = knowledge.propose(
            property_slug="property-a",
            topic="parking",
            title=title,
            content="Some durable operational rule about this topic.",
            audience=audience,
        )
        knowledge.decide(
            item.knowledge_ref, KnowledgeStatus.APPROVED, actor_user_id="user-admin-1"
        )

    visible = [item.title for item in knowledge.approved_for("property-a")]

    assert visible == ["Shared parking"]


# -- 3/4. consolidation and global promotion -------------------------------


def make(
    content,
    slug="property-a",
    topic="early_check_in",
    title="Early check-in",
    refs=("r1", "r2"),
    audience=GUEST_FACING,
    safety=SAFE,
):
    return Candidate(
        property_slug=slug,
        topic=topic,
        title=title,
        content=content,
        audience=audience,
        reason=None,
        safety_status=safety,
        safety_reasons=(),
        evidence_refs=refs,
        evidence_properties=frozenset({slug}) if slug else frozenset(),
        first_observed_at="2026-03-01T10:00:00",
        last_observed_at="2026-03-02T10:00:00",
    )


EARLY_A = "Early check-in depends on the previous checkout and on cleaning being done."
EARLY_B = (
    "Early check-in depends on whether the previous checkout and cleaning are done."
)


def test_near_duplicates_within_a_property_are_merged():
    merged = consolidate(
        [make(EARLY_A, refs=("r1", "r2")), make(EARLY_B, refs=("r3",))]
    )

    assert len(merged) == 1
    # Evidence combines: both were really observed.
    assert merged[0].evidence_count == 3


def test_unrelated_rules_are_not_merged():
    merged = consolidate(
        [
            make(EARLY_A),
            make(
                "Parking is shared between guests and is not allocated.",
                topic="parking",
                title="Parking",
            ),
        ]
    )

    assert len(merged) == 2


def test_a_guest_rule_and_an_internal_rule_are_never_merged():
    merged = consolidate(
        [
            make(EARLY_A, audience=GUEST_FACING),
            make(EARLY_A, audience=INTERNAL_OPERATION),
        ]
    )

    assert len(merged) == 2


def test_a_merged_rule_inherits_the_more_cautious_safety_status():
    merged = consolidate(
        [
            make(EARLY_A, refs=("r1", "r2", "r3"), safety=SAFE),
            make(EARLY_B, refs=("r4",), safety=REVIEW_NUMERIC_FACT),
        ]
    )

    assert merged[0].safety_status == REVIEW_NUMERIC_FACT


def test_agreement_across_enough_properties_becomes_global():
    candidates = [
        make(EARLY_A, slug=f"property-{n}") for n in range(MIN_PROPERTIES_FOR_GLOBAL)
    ]

    promoted, count = promote_global(candidates)

    assert count == 1

    globals_ = [item for item in promoted if item.property_slug is None]

    assert len(globals_) == 1
    assert globals_[0].distinct_property_count >= MIN_PROPERTIES_FOR_GLOBAL
    # The per-property copies are absorbed, not left alongside.
    assert all(item.property_slug is None for item in promoted)


def test_one_property_can_never_become_global():
    promoted, count = promote_global([make(EARLY_A), make(EARLY_B)])

    assert count == 0
    assert all(item.property_slug is not None for item in promoted)


def test_two_properties_are_below_the_threshold():
    _, count = promote_global(
        [make(EARLY_A, slug="property-a"), make(EARLY_A, slug="property-b")]
    )

    assert count == 0


def test_a_property_specific_fact_cannot_become_global():
    driveway = "The driveway is shared and fits guests parking in sequence."

    allowed, reason = may_be_global(
        [make(driveway, slug=f"property-{n}") for n in range(4)]
    )

    assert allowed is False
    assert reason == "property_specific_content"


def test_a_numeric_fact_cannot_become_global():
    allowed, reason = may_be_global(
        [
            make(EARLY_A, slug=f"property-{n}", safety=REVIEW_NUMERIC_FACT)
            for n in range(4)
        ]
    )

    assert allowed is False
    assert reason == "numeric_fact"


def test_contradictory_cross_property_evidence_blocks_promotion():
    cluster = [
        make("Early check-in is not guaranteed and depends on cleaning.", slug="a"),
        make("Early check-in is not guaranteed and depends on turnover.", slug="b"),
        make("Early check-in is guaranteed and always available to guests.", slug="c"),
    ]

    allowed, reason = may_be_global(cluster)

    assert allowed is False
    assert reason == "contradictory_evidence"


def test_a_clean_cross_property_cluster_is_allowed():
    allowed, reason = may_be_global(
        [make(EARLY_A, slug=f"property-{n}") for n in range(MIN_PROPERTIES_FOR_GLOBAL)]
    )

    assert allowed is True
    assert reason == "agreed_across_properties"


def test_the_global_threshold_is_conservative():
    # Documented as half the six-property portfolio. Two would be coincidence.
    assert MIN_PROPERTIES_FOR_GLOBAL == 3


# -- 7. re-distillation --------------------------------------------------


def test_clearing_proposed_preserves_decided_knowledge(knowledge):
    keep, _ = knowledge.propose(
        property_slug="property-a",
        topic="parking",
        title="Approved rule",
        content="Parking is shared between guests and is not allocated.",
        audience=GUEST_FACING,
    )
    knowledge.decide(
        keep.knowledge_ref, KnowledgeStatus.APPROVED, actor_user_id="user-admin-1"
    )

    refused, _ = knowledge.propose(
        property_slug="property-a",
        topic="parking",
        title="Rejected rule",
        content="Parking is always free for every guest at every property.",
    )
    knowledge.decide(
        refused.knowledge_ref, KnowledgeStatus.REJECTED, actor_user_id="user-admin-1"
    )

    knowledge.propose(
        property_slug="property-a",
        topic="parking",
        title="Stale candidate",
        content="Some candidate nobody has reviewed yet at all.",
    )

    cleared = knowledge.clear_proposed()

    assert cleared == 1

    remaining = {item.title: item.status for item in knowledge.list_knowledge()}

    assert remaining == {
        "Approved rule": "APPROVED",
        "Rejected rule": "REJECTED",
    }


def test_re_distillation_replaces_the_queue_and_keeps_approvals(archive, knowledge):
    keep, _ = knowledge.propose(
        property_slug="property-a",
        topic="parking",
        title="Owner approved rule",
        content="Parking is shared between guests and is not allocated.",
        audience=GUEST_FACING,
    )
    knowledge.decide(
        keep.knowledge_ref, KnowledgeStatus.APPROVED, actor_user_id="user-admin-1"
    )

    distil(archive, knowledge, FakeModel([candidate_json("Shared parking", GOOD)]))

    titles = {item.title: item.status for item in knowledge.list_knowledge()}

    assert titles["Owner approved rule"] == "APPROVED"
    assert knowledge.approved_for("property-a")[0].title == "Owner approved rule"


def test_distillation_records_audience_and_safety(archive, knowledge):
    numeric = candidate_json(
        "Parking capacity",
        "Parking is shared and the area fits two cars for guests.",
    )

    distil(archive, knowledge, FakeModel([numeric]))

    item = knowledge.list_knowledge()[0]

    assert item.safety_status == REVIEW_NUMERIC_FACT
    assert "numeric:capacity" in item.safety_reasons
    assert item.audience == GUEST_FACING


def test_candidate_output_exposes_the_review_fields():
    fields = set(make(EARLY_A).to_dict())

    assert {
        "scope",
        "property_slug",
        "topic",
        "audience",
        "content",
        "evidence_count",
        "distinct_property_count",
        "safety_status",
        "safety_reasons",
        "first_observed_at",
        "last_observed_at",
    } <= fields


def test_no_guest_text_reaches_candidate_output(archive, knowledge):
    distil(archive, knowledge, FakeModel([candidate_json("Shared parking", GOOD)]))

    body = json.dumps([item.to_dict() for item in knowledge.list_knowledge()])

    assert "Where do we park" not in body
    assert "evidence_refs" not in body


# ==========================================================================
# Console V1: manual authoring, supersession, conflicts
# ==========================================================================

from app.knowledge_conflicts import (
    DUPLICATE_SCOPE_TOPIC,
    OPPOSING_STANCE,
    find_conflicts,
)

PARKING = "Parking is shared between guests and is not allocated to a unit."


def test_manual_knowledge_is_approved_on_creation(knowledge):
    item = knowledge.create_manual(
        property_slug="property-a",
        topic="parking",
        title="Shared parking",
        content=PARKING,
        audience=GUEST_FACING,
        actor_user_id="user-admin-1",
    )

    # The owner writing the sentence *is* the review.
    assert item.status == KnowledgeStatus.APPROVED.value
    assert item.source_type == "MANUAL"
    assert item.decided_by_user_id == "user-admin-1"
    assert knowledge.approved_for("property-a")[0].title == "Shared parking"


def test_manual_knowledge_refuses_a_duplicate(knowledge):
    for _ in range(1):
        knowledge.create_manual(
            property_slug="property-a",
            topic="parking",
            title="Shared parking",
            content=PARKING,
            audience=GUEST_FACING,
            actor_user_id="user-admin-1",
        )

    with pytest.raises(ValueError):
        knowledge.create_manual(
            property_slug="property-a",
            topic="parking",
            title="Shared parking",
            content=PARKING,
            audience=GUEST_FACING,
            actor_user_id="user-admin-1",
        )


def test_manual_internal_knowledge_stays_out_of_drafting(knowledge):
    knowledge.create_manual(
        property_slug="property-a",
        topic="cancellation",
        title="Internal record",
        content="Staff should treat the internal record as authoritative here.",
        audience=INTERNAL_OPERATION,
        actor_user_id="user-admin-1",
    )

    assert knowledge.approved_for("property-a") == []


# -- supersession ----------------------------------------------------------


def approved_rule(knowledge, title="Shared parking", content=PARKING):
    return knowledge.create_manual(
        property_slug="property-a",
        topic="parking",
        title=title,
        content=content,
        audience=GUEST_FACING,
        actor_user_id="user-admin-1",
    )


def test_superseding_keeps_the_old_wording(knowledge):
    original = approved_rule(knowledge)

    old, replacement = knowledge.supersede(
        original.knowledge_ref,
        actor_user_id="user-admin-2",
        content="Parking is shared; confirm the arrangement before arrival.",
    )

    assert old.status == KnowledgeStatus.SUPERSEDED.value
    assert old.content == PARKING
    assert old.decided_by_user_id == "user-admin-2"

    assert replacement.status == KnowledgeStatus.APPROVED.value
    assert "confirm the arrangement" in replacement.content

    # Both rows survive: history is not rewritten.
    assert len(knowledge.list_knowledge()) == 2


def test_only_the_replacement_reaches_drafting(knowledge):
    original = approved_rule(knowledge)

    knowledge.supersede(
        original.knowledge_ref,
        actor_user_id="user-admin-1",
        title="Shared parking, confirmed on arrival",
        content="Parking is shared; confirm the arrangement before arrival.",
    )

    live = knowledge.approved_for("property-a")

    assert len(live) == 1
    assert "confirm the arrangement" in live[0].content


def test_only_approved_knowledge_can_be_superseded(knowledge):
    item, _ = knowledge.propose(
        property_slug="property-a",
        topic="parking",
        title="Shared parking",
        content=PARKING,
    )

    with pytest.raises(ValueError):
        knowledge.supersede(
            item.knowledge_ref, actor_user_id="user-admin-1", content="Anything else."
        )


def test_an_identical_replacement_is_refused(knowledge):
    original = approved_rule(knowledge)

    with pytest.raises(ValueError):
        knowledge.supersede(
            original.knowledge_ref,
            actor_user_id="user-admin-1",
            content=PARKING,
        )


# -- conflicts -------------------------------------------------------------


def test_two_approved_rules_at_one_scope_and_topic_are_surfaced(knowledge):
    approved_rule(knowledge, title="Parking rule one")
    approved_rule(
        knowledge, title="Parking rule two", content="Parking is shared here."
    )

    conflicts = find_conflicts(knowledge.list_knowledge())

    assert len(conflicts) == 1
    assert conflicts[0].topic == "parking"
    assert conflicts[0].reason == DUPLICATE_SCOPE_TOPIC
    assert len(conflicts[0].knowledge_refs) == 2


def test_opposing_stances_are_reported_as_such(knowledge):
    approved_rule(
        knowledge,
        title="Early check-in is limited",
        content="Early check-in is not guaranteed and depends on the checkout.",
    )
    approved_rule(
        knowledge,
        title="Early check-in is open",
        content="Early check-in is guaranteed and always available to guests.",
    )

    conflicts = find_conflicts(knowledge.list_knowledge())

    assert conflicts[0].reason == OPPOSING_STANCE


def test_a_global_rule_beside_a_property_rule_is_not_a_conflict(knowledge):
    approved_rule(knowledge)

    knowledge.create_manual(
        property_slug=None,
        topic="parking",
        title="Global parking rule",
        content="Parking arrangements vary and should be confirmed per property.",
        audience=GUEST_FACING,
        actor_user_id="user-admin-1",
    )

    # Different scopes. The property rule is simply the more specific answer.
    assert find_conflicts(knowledge.list_knowledge()) == []


def test_proposals_never_count_as_conflicts(knowledge):
    approved_rule(knowledge)

    knowledge.propose(
        property_slug="property-a",
        topic="parking",
        title="A candidate",
        content="Parking might work differently according to this candidate.",
    )

    assert find_conflicts(knowledge.list_knowledge()) == []


# -- API -------------------------------------------------------------------


def test_admin_can_author_knowledge_through_the_api(knowledge_api):
    client = knowledge_api.client("ADMIN")

    response = client.post(
        "/knowledge",
        json={
            "property_slug": "property-a",
            "topic": "house_rules",
            "title": "Quiet hours",
            "content": "Please keep noise down late in the evening.",
            "audience": "GUEST_FACING",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "APPROVED"
    assert response.json()["source_type"] == "MANUAL"

    events = {
        event["event_type"] for event in client.get("/audit/events?limit=50").json()
    }

    assert {"KNOWLEDGE_PROPOSED", "KNOWLEDGE_APPROVED"} <= events


def test_authoring_knowledge_requires_admin(knowledge_api):
    response = knowledge_api.client("APPROVER").post(
        "/knowledge",
        json={
            "topic": "house_rules",
            "title": "Quiet hours",
            "content": "Please keep noise down late in the evening.",
            "audience": "GUEST_FACING",
        },
    )

    assert response.status_code == 403


def test_an_unknown_audience_is_refused(knowledge_api):
    response = knowledge_api.client("ADMIN").post(
        "/knowledge",
        json={
            "topic": "house_rules",
            "title": "Quiet hours",
            "content": "Please keep noise down late in the evening.",
            "audience": "EVERYONE",
        },
    )

    assert response.status_code == 400


def test_supersede_through_the_api_is_audited(knowledge_api):
    client = knowledge_api.client("ADMIN")

    created = client.post(
        "/knowledge",
        json={
            "property_slug": "property-a",
            "topic": "parking",
            "title": "Shared parking",
            "content": PARKING,
            "audience": "GUEST_FACING",
        },
    ).json()

    response = client.post(
        f"/knowledge/{created['knowledge_ref']}/supersede",
        json={"content": "Parking is shared; confirm before arrival."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "APPROVED"

    events = client.get("/audit/events?event_type=KNOWLEDGE_SUPERSEDED").json()

    assert len(events) == 1
    assert events[0]["details"]["previous_content"] == PARKING
    assert events[0]["actor_user_id"]


def test_supersede_requires_admin(knowledge_api):
    response = knowledge_api.client("OPERATOR").post(
        f"/knowledge/{knowledge_api.knowledge_ref}/supersede",
        json={"content": "Something else entirely."},
    )

    assert response.status_code == 403


def test_the_list_endpoint_reports_conflicts(knowledge_api):
    client = knowledge_api.client("ADMIN")

    for title in ("Parking rule one", "Parking rule two"):
        client.post(
            "/knowledge",
            json={
                "property_slug": "property-a",
                "topic": "parking",
                "title": title,
                "content": PARKING if title.endswith("one") else "Parking is shared.",
                "audience": "GUEST_FACING",
            },
        )

    payload = client.get("/knowledge").json()

    assert len(payload["conflicts"]) == 1
    assert payload["conflicts"][0]["topic"] == "parking"
    assert payload["conflicts"][0]["message"]


def test_approval_after_edit_uses_the_edited_wording(knowledge_api):
    client = knowledge_api.client("ADMIN")

    ref = knowledge_api.knowledge_ref

    client.patch(
        f"/knowledge/{ref}", json={"content": "Parking is shared; confirm it."}
    )
    client.post(f"/knowledge/{ref}/approve")

    live = knowledge_api.store.approved_for("renovated-2nd-floor-home")

    assert len(live) == 1
    assert live[0].content == "Parking is shared; confirm it."


def test_console_approval_reaches_the_drafting_path(knowledge_api):
    """The whole point of the console: approving here changes what a draft sees."""
    from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools

    store = knowledge_api.store

    assert store.approved_for("renovated-2nd-floor-home") == []

    knowledge_api.client("ADMIN").post(
        f"/knowledge/{knowledge_api.knowledge_ref}/approve"
    )

    tools = LodgifyMessagingTools(inbox=None, knowledge=store)

    visible = tools.approved_knowledge("renovated-2nd-floor-home")

    assert len(visible) == 1
    assert visible[0]["title"] == "Shared parking"
    # The drafting projection carries no review metadata.
    assert set(visible[0]) == {"topic", "scope", "title", "content"}


def test_approved_internal_knowledge_never_reaches_drafting(knowledge_api):
    from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools

    client = knowledge_api.client("ADMIN")

    created = client.post(
        "/knowledge",
        json={
            "property_slug": "renovated-2nd-floor-home",
            "topic": "cancellation",
            "title": "Internal record",
            "content": "Staff should treat the internal record as authoritative.",
            "audience": "INTERNAL_OPERATION",
        },
    ).json()

    assert created["status"] == "APPROVED"

    tools = LodgifyMessagingTools(inbox=None, knowledge=knowledge_api.store)

    assert tools.approved_knowledge("renovated-2nd-floor-home") == []
