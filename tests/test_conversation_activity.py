"""The conversation activity index: a metadata snapshot, never an archive."""

from app.connectors.lodgify.messaging_models import ConversationStatus
from app.conversation_activity import ConversationActivityStore


def activity(store, ref="PH-AAAA1111", at="2026-09-03T12:06:33", **kwargs):
    return store.upsert(
        conversation_ref=ref,
        conversation_fingerprint=kwargs.pop("fingerprint", "fp-1"),
        status=kwargs.pop("status", ConversationStatus.NEEDS_ATTENTION.value),
        last_message_at=at,
        last_message_sender=kwargs.pop("sender", "Renter"),
        message_count=kwargs.pop("message_count", 3),
        property_slug=kwargs.pop("property_slug", "renovated-2nd-floor-home"),
        source=kwargs.pop("source", "BookingCom"),
        booking_status=kwargs.pop("booking_status", "Booked"),
    )


def test_an_upsert_stores_one_row(database):
    store = ConversationActivityStore(database=database)

    activity(store)

    rows = store.all_activity()

    assert len(rows) == 1
    assert rows[0].conversation_ref == "PH-AAAA1111"
    assert rows[0].last_message_at == "2026-09-03T12:06:33"


def test_a_repeated_upsert_updates_the_same_row(database):
    """A re-delivered webhook must not create a second conversation."""
    store = ConversationActivityStore(database=database)

    first = activity(store)
    activity(store, at="2026-09-03T14:00:00", fingerprint="fp-2")

    rows = store.all_activity()

    assert len(rows) == 1
    assert rows[0].last_message_at == "2026-09-03T14:00:00"
    assert rows[0].conversation_fingerprint == "fp-2"
    # first_seen_at is when we first learned of it, and never moves.
    assert rows[0].first_seen_at == first.first_seen_at
    assert rows[0].last_refreshed_at >= first.last_refreshed_at


def test_needs_attention_is_derived_not_stored(database):
    store = ConversationActivityStore(database=database)

    activity(store, status=ConversationStatus.NEEDS_ATTENTION.value)
    assert store.for_conversation("PH-AAAA1111").needs_attention is True

    activity(store, status=ConversationStatus.RESPONDED.value)
    assert store.for_conversation("PH-AAAA1111").needs_attention is False


def test_the_row_projection_carries_no_guest_text_or_provider_ids(database):
    store = ConversationActivityStore(database=database)

    row = activity(store).to_row()

    for forbidden in (
        "booking_id",
        "thread_uid",
        "guest_name",
        "guest_email",
        "guest_phone",
        "last_message_excerpt",
        "message",
    ):
        assert forbidden not in row


def test_an_unknown_conversation_reads_as_none(database):
    store = ConversationActivityStore(database=database)

    assert store.for_conversation("PH-NOTHERE1") is None


def test_the_store_never_touches_the_development_database(
    database, development_database_path
):
    """Isolation proof: the store writes only to the injected database.

    Handles a clean checkout, where ./agentops.db does not exist at all. There
    the proof is that the store did not bring it into being -- stat-ing it
    unconditionally would fail on a fresh clone, which is a defect in the test
    rather than a real isolation failure. Mirrors the guard in
    tests/test_migrations.py::test_the_development_database_is_untouched.
    """
    existed = development_database_path.exists()

    before = (
        (
            development_database_path.stat().st_size,
            development_database_path.stat().st_mtime_ns,
        )
        if existed
        else None
    )

    activity(ConversationActivityStore(database=database))

    if not existed:
        assert not development_database_path.exists()

        return

    after = (
        development_database_path.stat().st_size,
        development_database_path.stat().st_mtime_ns,
    )

    assert before == after
