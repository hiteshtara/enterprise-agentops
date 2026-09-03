"""Historical reply extraction, privacy, storage and retrieval.

Every conversation in this file is invented. No real guest message, name,
address or booking appears anywhere -- the historical index is private
operational data and must not be reproduced in a repository.

No test reaches Lodgify or OpenAI.
"""

import json

import pytest

from app.historical_replies import (
    HistoricalReplyStore,
    IndexReport,
    extract_exchanges,
    fingerprint,
    is_system_message,
    redact,
    topics_for,
)
from app.reply_retrieval import (
    MAX_LIMIT,
    HistoricalReplyRetriever,
    tokenise,
)


def guest(text: str, at: str = "2026-03-01T10:00:00", subject: str = "Question"):
    return {"sender": "Renter", "message": text, "created_at": at, "subject": subject}


def owner(text: str, at: str = "2026-03-01T11:00:00", subject: str = "Re: Question"):
    return {"sender": "Owner", "message": text, "created_at": at, "subject": subject}


@pytest.fixture
def store(database):
    return HistoricalReplyStore(database=database)


# -- 1/2/3/4. extraction ---------------------------------------------------


def test_a_simple_pair_becomes_one_example():
    exchanges = extract_exchanges(
        [guest("Is there parking?"), owner("Yes, shared parking out front.")],
        property_slug="renovated-2nd-floor-home",
        source="BookingCom",
    )

    assert len(exchanges) == 1

    exchange = exchanges[0]

    assert exchange.guest_text == "Is there parking?"
    assert exchange.owner_text == "Yes, shared parking out front."
    assert exchange.property_slug == "renovated-2nd-floor-home"
    assert exchange.source == "BookingCom"
    assert exchange.created_at == "2026-03-01T10:00:00"


def test_consecutive_guest_messages_are_joined_into_one_question():
    exchanges = extract_exchanges(
        [
            guest("Hi!", "2026-03-01T09:00:00"),
            guest("Is there parking?", "2026-03-01T09:01:00"),
            guest("And a lift?", "2026-03-01T09:02:00"),
            owner("Parking is shared, and there is no lift."),
        ]
    )

    assert len(exchanges) == 1
    assert exchanges[0].guest_text == "Hi! Is there parking? And a lift?"
    # Dated from the first message of the run, not the last.
    assert exchanges[0].created_at == "2026-03-01T09:00:00"


def test_consecutive_owner_messages_are_joined_into_one_answer():
    exchanges = extract_exchanges(
        [
            guest("Is there parking?"),
            owner("Yes.", "2026-03-01T11:00:00"),
            owner("It is shared with the other unit.", "2026-03-01T11:01:00"),
        ]
    )

    assert len(exchanges) == 1
    assert exchanges[0].owner_text == "Yes. It is shared with the other unit."


def test_an_unanswered_question_produces_no_example():
    assert extract_exchanges([guest("Is there parking?")]) == []


def test_owner_messages_before_any_guest_turn_are_skipped():
    exchanges = extract_exchanges(
        [
            owner("Welcome! Here are the directions."),
            guest("Thanks, is there parking?"),
            owner("Yes, shared."),
        ]
    )

    assert len(exchanges) == 1
    assert exchanges[0].guest_text == "Thanks, is there parking?"


def test_multiple_turns_produce_multiple_examples_paired_correctly():
    exchanges = extract_exchanges(
        [
            guest("Is there parking?", "2026-03-01T09:00:00"),
            owner("Yes, shared.", "2026-03-01T09:30:00"),
            guest("Great. And wifi?", "2026-03-02T09:00:00"),
            owner("Wifi details are in the welcome note.", "2026-03-02T09:30:00"),
        ]
    )

    assert len(exchanges) == 2
    assert exchanges[0].guest_text == "Is there parking?"
    assert exchanges[0].owner_text == "Yes, shared."
    assert exchanges[1].guest_text == "Great. And wifi?"
    assert exchanges[1].owner_text == "Wifi details are in the welcome note."


def test_empty_and_whitespace_messages_are_ignored():
    assert extract_exchanges([guest("   "), owner("Yes.")]) == []
    assert extract_exchanges([guest("Is there parking?"), owner("  ")]) == []


def test_system_notifications_are_not_treated_as_the_owners_voice():
    assert is_system_message("New Confirmed Booking: 4 Nights") is True
    assert is_system_message("Your quote for the apartment") is True
    assert is_system_message("Re: Question") is False

    exchanges = extract_exchanges(
        [
            guest("Is there parking?"),
            owner("Hello, your booking is confirmed.", subject="New Confirmed Booking"),
        ]
    )

    # Only Lodgify's template followed the question, so there is no precedent.
    assert exchanges == []


def test_a_real_reply_survives_alongside_a_system_notification():
    exchanges = extract_exchanges(
        [
            guest("Is there parking?"),
            owner("Automated.", subject="Payment received"),
            owner("Yes, shared parking.", subject="Re: parking"),
        ]
    )

    assert len(exchanges) == 1
    assert exchanges[0].owner_text == "Yes, shared parking."


# -- 5/6/7. privacy --------------------------------------------------------


def test_contact_details_are_removed_before_persistence():
    text = (
        "Hi, I'm Jordan Alvarez, reach me at jordan.alvarez@example.invalid "
        "or +1 555 010 9999. The door code was 48213."
    )

    cleaned = redact(
        text, identities=("Jordan Alvarez", "jordan.alvarez@example.invalid")
    )

    assert "Jordan" not in cleaned
    assert "Alvarez" not in cleaned
    assert "example.invalid" not in cleaned
    assert "555" not in cleaned
    assert "48213" not in cleaned
    assert "[redacted]" in cleaned

    # The useful part of the sentence survives.
    assert "door code" in cleaned


def test_redaction_uses_the_known_identity_not_only_patterns():
    # A bare first name matches no pattern; it is removed because the thread
    # told us who the guest is.
    cleaned = redact("Thanks, Marisol here again", identities=("Marisol Vega",))

    assert "Marisol" not in cleaned


def test_urls_and_confirmation_codes_are_removed():
    cleaned = redact("See https://example.invalid/x and code HMABCD1234")

    assert "https://" not in cleaned
    assert "HMABCD1234" not in cleaned


def test_extraction_applies_redaction_to_both_sides():
    exchanges = extract_exchanges(
        [
            guest("It's Priya, my number is +1 555 010 2222"),
            owner("Thanks Priya, I'll text you on +1 555 010 3333"),
        ],
        identities=("Priya",),
    )

    assert len(exchanges) == 1

    body = exchanges[0].guest_text + exchanges[0].owner_text

    assert "Priya" not in body
    assert "555" not in body


def test_an_exchange_carries_no_provider_identifier(store):
    exchanges = extract_exchanges(
        [guest("Is there parking?"), owner("Yes, shared.")],
        property_slug="renovated-2nd-floor-home",
        source="BookingCom",
    )

    store.upsert(exchanges)

    body = json.dumps(store.all_examples())

    for forbidden in (
        "booking_id",
        "thread_uid",
        "guest_email",
        "guest_name",
        "source_text",
    ):
        assert forbidden not in body


def test_source_text_is_never_read_by_extraction():
    # Extraction is handed sanitized messages only; there is no code path that
    # could reach a booking's free-text source field.
    exchanges = extract_exchanges(
        [guest("Is there parking?"), owner("Yes.")],
        source="BookingCom",
    )

    assert exchanges[0].source == "BookingCom"
    assert "listingId" not in json.dumps(exchanges[0].to_dict())


def test_stored_columns_are_only_the_agreed_fields(store):
    store.upsert(
        extract_exchanges(
            [guest("Is there parking?"), owner("Yes, shared.")],
            property_slug="renovated-2nd-floor-home",
        )
    )

    assert set(store.all_examples()[0]) == {
        "example_ref",
        "property_slug",
        "source",
        "guest_text",
        "owner_text",
        "topics",
        "created_at",
    }


# -- 8/9. idempotency ------------------------------------------------------


def test_the_same_exchange_hashes_the_same():
    first = fingerprint("a", "q", "r", "2026-03-01T10:00:00")
    again = fingerprint("a", "q", "r", "2026-03-01T18:00:00")

    # Same day, same content -- one precedent, however often it is re-read.
    assert first == again
    assert first != fingerprint("a", "q", "r", "2026-03-02T10:00:00")
    assert first != fingerprint("b", "q", "r", "2026-03-01T10:00:00")


def test_reindexing_creates_no_duplicates(store):
    exchanges = extract_exchanges(
        [guest("Is there parking?"), owner("Yes, shared.")],
        property_slug="renovated-2nd-floor-home",
    )

    created, updated = store.upsert(exchanges)

    assert (created, updated) == (1, 0)

    created, updated = store.upsert(exchanges)

    assert (created, updated) == (0, 1)
    assert store.count() == 1


def test_extraction_deduplicates_within_one_thread():
    # The identical exchange twice on the same day is one precedent.
    exchanges = extract_exchanges(
        [
            guest("Is there parking?", "2026-03-01T09:00:00"),
            owner("Yes, shared.", "2026-03-01T09:05:00"),
            guest("Is there parking?", "2026-03-01T10:00:00"),
            owner("Yes, shared.", "2026-03-01T10:05:00"),
        ]
    )

    assert len(exchanges) == 1


# -- 10. topics ------------------------------------------------------------


def test_topics_are_derived_deterministically():
    assert "early_check_in" in topics_for("Can we check in early?")
    assert "parking" in topics_for("Where do we park the car?")
    assert "wifi" in topics_for("What is the wifi password?")
    assert topics_for("hello there") == []


def test_paraphrase_reaches_the_same_topic():
    # No shared content words with "is early check-in possible", but the same
    # subject -- which is the whole reason topics exist.
    assert "early_check_in" in topics_for("can we get in before 3")


# -- 11-15. retrieval ------------------------------------------------------


PARKING = (
    [guest("Is there parking at the property?")],
    [owner("Parking is shared out front, no extra charge.")],
)

CHECK_IN = (
    [guest("Can we check in early?")],
    [owner("If there is no checkout that day, usually yes. I'll confirm closer.")],
)

WIFI = (
    [guest("What is the wifi password?")],
    [owner("It is on the card by the router.")],
)


def seed(store, *specs):
    for messages, slug in specs:
        store.upsert(extract_exchanges(messages, property_slug=slug))


def test_retrieval_returns_the_relevant_example(store):
    seed(
        store,
        (
            [
                guest("Is there parking at the property?"),
                owner("Parking is shared, no charge."),
            ],
            "a",
        ),
        (
            [guest("What is the wifi password?"), owner("On the card by the router.")],
            "a",
        ),
    )

    found = HistoricalReplyRetriever(store).find("Where can we park?")

    assert found
    assert "parking" in found[0].owner_example.lower()


def test_an_unrelated_question_returns_nothing(store):
    seed(store, ([guest("Is there parking?"), owner("Shared, no charge.")], "a"))

    assert (
        HistoricalReplyRetriever(store).find("Do you allow scuba diving lessons") == []
    )


def test_an_empty_index_returns_nothing(store):
    assert HistoricalReplyRetriever(store).find("Is there parking?") == []


def test_same_property_wins_among_comparable_matches(store):
    seed(
        store,
        (
            [guest("Is there parking here?"), owner("Answer from property A.")],
            "property-a",
        ),
        (
            [guest("Is there parking here?"), owner("Answer from property B.")],
            "property-b",
        ),
    )

    found = HistoricalReplyRetriever(store).find(
        "Is there parking here?",
        property_slug="property-b",
    )

    assert found[0].property_slug == "property-b"


def test_a_clearly_better_cross_property_match_beats_a_weak_local_one(store):
    seed(
        store,
        (
            [guest("What is the wifi password?"), owner("Weak local match.")],
            "property-a",
        ),
        (
            [
                guest("Can we check in early before 3pm?"),
                owner("Strong cross-property match."),
            ],
            "property-b",
        ),
    )

    found = HistoricalReplyRetriever(store).find(
        "Can we check in early before 3pm?",
        property_slug="property-a",
    )

    # The same-property bonus is a nudge, not a veto.
    assert found[0].owner_example == "Strong cross-property match."


def test_results_are_bounded(store):
    for index in range(12):
        store.upsert(
            extract_exchanges(
                [
                    guest(
                        f"Is there parking spot {index}?",
                        f"2026-03-{index + 1:02d}T09:00:00",
                    ),
                    owner("Parking is shared."),
                ]
            )
        )

    assert len(HistoricalReplyRetriever(store).find("parking", limit=3)) == 3
    assert len(HistoricalReplyRetriever(store).find("parking", limit=99)) <= MAX_LIMIT


def test_retrieval_result_shape(store):
    seed(
        store, ([guest("Is there parking?"), owner("Shared, no charge.")], "property-a")
    )

    result = HistoricalReplyRetriever(store).find(
        "parking", property_slug="property-a"
    )[0]

    assert set(result.to_dict()) == {
        "guest_example",
        "owner_example",
        "property_slug",
        "similarity",
    }


def test_tokenise_drops_stopwords_and_punctuation():
    assert tokenise("Is there a parking spot?") == ["parking", "spot"]
    assert tokenise("") == []


def test_index_report_carries_counts_only():
    report = IndexReport(bookings_scanned=143, threads_read=139, created=398)

    assert set(report.to_dict()) == {
        "bookings_scanned",
        "threads_read",
        "thread_errors",
        "exchanges_extracted",
        "created",
        "updated",
        "skipped",
    }

    assert all(isinstance(value, int) for value in report.to_dict().values())
