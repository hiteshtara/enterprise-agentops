"""Historical examples reaching the drafting layer -- and staying subordinate.

The risk this milestone introduces is not that retrieval fails. It is that a
real past reply gets treated as a current fact. These tests pin the gating, the
labelling, the authority order, and the fallback.

All conversations are invented. No test reaches Lodgify or OpenAI.
"""

import json

import pytest

from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools
from app.connectors.lodgify.refs import conversation_ref_for
from app.historical_replies import HistoricalReplyStore, extract_exchanges
from app.hospitality import AUTHORITY_ORDER, HISTORICAL_EXAMPLE_CAVEAT
from app.reply_retrieval import HistoricalReplyRetriever
from tests.lodgify_fakes import THREAD_A, FakeLodgify, booking, message, thread

REF = conversation_ref_for(1001)

OPEN_QUESTION = message(
    "m-guest-1",
    "Renter",
    "Can we check in early, before 3pm?",
    "2026-09-01T10:00:00",
    message_status=None,
    route=None,
)

CLOSING = message(
    "m-guest-2",
    "Renter",
    "Thank you!",
    "2026-09-01T12:00:00",
    message_status=None,
    route=None,
)

OWNER_REPLY = message(
    "m-owner-1",
    "Owner",
    "I'll confirm closer to your stay.",
    "2026-09-01T11:00:00",
)


def guest(text, at="2026-03-01T10:00:00"):
    return {"sender": "Renter", "message": text, "created_at": at, "subject": "Q"}


def owner(text, at="2026-03-01T11:00:00"):
    return {"sender": "Owner", "message": text, "created_at": at, "subject": "A"}


@pytest.fixture
def store(database):
    store = HistoricalReplyStore(database=database)

    store.upsert(
        extract_exchanges(
            [
                guest("Can we check in early?"),
                owner("If there's no checkout that day we can usually manage it."),
            ],
            property_slug="renovated-3rd-floor-retreat-3-beds-roslindale-village",
        )
    )

    return store


def tools_for(messages, store=None):
    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        threads={THREAD_A: thread(THREAD_A, messages)},
    )

    return LodgifyMessagingTools(
        fake.inbox(),
        retriever=HistoricalReplyRetriever(store) if store else None,
    )


# -- 16/17. gating ---------------------------------------------------------


def test_reply_needed_retrieves_examples(store):
    result = tools_for([OPEN_QUESTION], store).get_guest_conversation(REF)

    assert result["reply_guidance"]["conversation_state"]["suggested_outcome"] == (
        "reply_needed"
    )

    assert result["historical_examples"]["examples"]


def test_no_reply_needed_retrieves_nothing(store):
    result = tools_for(
        [OPEN_QUESTION, OWNER_REPLY, CLOSING], store
    ).get_guest_conversation(REF)

    assert result["reply_guidance"]["conversation_state"]["suggested_outcome"] == (
        "no_reply_needed"
    )

    # No retrieval, no examples, and nothing for the model to read past.
    assert "historical_examples" not in result


def test_an_already_answered_thread_retrieves_nothing(store):
    result = tools_for([OPEN_QUESTION, OWNER_REPLY], store).get_guest_conversation(REF)

    assert result["reply_guidance"]["conversation_state"]["suggested_outcome"] == (
        "already_replied"
    )

    assert "historical_examples" not in result


# -- 18/19/20. what reaches the model --------------------------------------


def test_examples_reach_model_context_with_both_sides(store):
    block = tools_for([OPEN_QUESTION], store).get_guest_conversation(REF)[
        "historical_examples"
    ]

    example = block["examples"][0]

    assert "check in early" in example["guest_example"].lower()
    assert "checkout" in example["owner_example"].lower()
    assert example["similarity"] > 0


def test_examples_are_labelled_non_authoritative(store):
    block = tools_for([OPEN_QUESTION], store).get_guest_conversation(REF)[
        "historical_examples"
    ]

    assert block["how_to_use"] == HISTORICAL_EXAMPLE_CAVEAT

    caveat = block["how_to_use"].lower()

    assert "style and precedent" in caveat or "copy the voice" in caveat
    assert "may be months old" in caveat
    assert "never reproduce one" in caveat

    # And the rank is stated next to the examples, not only in the guidance.
    assert "rank 3 of 4" in block["authority"].lower()


def test_the_authority_order_puts_current_data_first_and_history_third(store):
    order = tools_for([OPEN_QUESTION], store).get_guest_conversation(REF)[
        "reply_guidance"
    ]["authority_order"]

    assert len(order) == 4
    assert order[0].startswith("1. CURRENT AUTHORITATIVE DATA")
    assert order[1].startswith("2. THE CURRENT CONVERSATION")
    assert order[2].startswith("3. HISTORICAL EXAMPLES")
    assert order[3].startswith("4. YOUR OWN GENERAL KNOWLEDGE")

    assert list(AUTHORITY_ORDER) == order


def test_a_historical_fact_cannot_outrank_current_policy(database):
    """A real past reply that contradicts current policy still arrives ranked
    below it, with the current rule intact and the contradiction called out."""
    store = HistoricalReplyStore(database=database)

    store.upsert(
        extract_exchanges(
            [guest("Is parking free?"), owner("Yes, parking is free.")],
            property_slug="renovated-3rd-floor-retreat-3-beds-roslindale-village",
        )
    )

    parking_question = message(
        "m-parking",
        "Renter",
        "Is parking free at the property?",
        "2026-09-01T10:00:00",
        message_status=None,
        route=None,
    )

    result = tools_for([parking_question], store).get_guest_conversation(REF)

    # The stale example is retrieved -- it is genuinely how the owner writes...
    assert result["historical_examples"]["examples"]

    # ...but the current parking rule is present and outranks it, and the
    # caveat names this exact failure mode.
    rules = {rule["topic"] for rule in result["reply_guidance"]["rules"]}

    assert "parking" in rules
    assert (
        "parking is free"
        in result["reply_guidance"]["historical_examples_caveat"].lower()
    )
    assert result["reply_guidance"]["authority_order"][0].startswith("1. CURRENT")


def test_the_caveat_forbids_copying_specifics(store):
    caveat = HISTORICAL_EXAMPLE_CAVEAT.lower()

    for forbidden in ("name", "date", "price", "access code", "promise"):
        assert forbidden in caveat


# -- 21. fallback ----------------------------------------------------------


def test_drafting_works_with_no_retriever_configured():
    result = tools_for([OPEN_QUESTION]).get_guest_conversation(REF)

    assert result["ok"] is True
    assert result["reply_guidance"]["conversation_state"]["suggested_outcome"] == (
        "reply_needed"
    )
    assert "historical_examples" not in result


def test_a_failing_retriever_does_not_break_drafting(store):
    class Broken:
        def find(self, **_):
            raise RuntimeError("index unavailable")

    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        threads={THREAD_A: thread(THREAD_A, [OPEN_QUESTION])},
    )

    result = LodgifyMessagingTools(
        fake.inbox(), retriever=Broken()
    ).get_guest_conversation(REF)

    # Enrichment failed; drafting is unaffected.
    assert result["ok"] is True
    assert result["reply_guidance"]
    assert "historical_examples" not in result


def test_an_empty_index_yields_no_examples_but_a_working_conversation(database):
    result = tools_for(
        [OPEN_QUESTION], HistoricalReplyStore(database=database)
    ).get_guest_conversation(REF)

    assert result["ok"] is True
    assert "historical_examples" not in result


# -- 22. leakage -----------------------------------------------------------


def test_historical_content_carries_no_provider_identifier(store):
    body = json.dumps(tools_for([OPEN_QUESTION], store).get_guest_conversation(REF))

    assert THREAD_A not in body
    assert "1001" not in body
    assert "thread_uid" not in body
    assert "booking_id" not in body
    assert "fixture.guest@example.invalid" not in body


def test_historical_examples_are_not_audited_as_actions(database, agent_factory):
    """Retrieval is context, not an action. The audit trail records what the
    agent *did* -- it is not a log of everything it read."""
    from tests.fakes import ScriptedModelProvider, final_response, tool_response

    store = HistoricalReplyStore(database=database)

    store.upsert(
        extract_exchanges(
            [guest("Can we check in early?"), owner("Usually yes, I'll confirm.")]
        )
    )

    fake = FakeLodgify(
        bookings=[booking(1001, THREAD_A)],
        threads={THREAD_A: thread(THREAD_A, [OPEN_QUESTION])},
    )

    from app.migration_store import MigrationBatchStore
    from app.tool_setup import build_tool_registry

    registry = build_tool_registry(
        migration_store=MigrationBatchStore(database=database),
        lodgify_messaging=LodgifyMessagingTools(
            fake.inbox(), retriever=HistoricalReplyRetriever(store)
        ),
    )

    model = ScriptedModelProvider(
        [
            tool_response("get_guest_conversation", {"conversation_ref": REF}),
            final_response("Draft written."),
        ]
    )

    agent = agent_factory(model, tool_registry=registry)

    result = agent.run("Draft a reply.", actor_user_id="tester")

    assert result["status"] == "COMPLETED"

    from app.audit_store import AuditStore

    events = AuditStore(database=database).list_events(limit=50)

    # No audit event exists for "retrieved examples" -- audit is about actions.
    assert not any("historical" in event["event_type"].lower() for event in events)
