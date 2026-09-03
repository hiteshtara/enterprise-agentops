"""Rendering the Inbox page from the persisted activity index.

Ordering comes from the index, so a conversation the index has never seen is
not on the page however live it is -- seeding belongs to the preparation cycle
alone (`prepare_activity_index`), and the request-count reasons for that are
covered in `tests/test_activity_index_ordering.py`. Every test here that wants
a live booking on the page therefore remembers it first, which is what a
preparation cycle would have done.
"""

from app.connectors.lodgify.messaging_client import INBOX_STAY_FILTERS
from app.connectors.lodgify.messaging_models import ConversationStatus
from app.connectors.lodgify.refs import conversation_ref_for
from app.conversation_activity import ConversationActivityStore
from app.inbox_view import build_inbox
from tests.lodgify_fakes import FakeLodgify, booking, message, thread


def live_fake(entries):
    bookings, threads = [], {}

    for booking_id, uid, at, sender in entries:
        bookings.append(booking(booking_id, uid))
        threads[uid] = thread(
            uid, [message(f"m-{uid}", sender, "Invented fixture text.", at)]
        )

    return FakeLodgify(bookings=bookings, threads=threads)


def historic_fake(live_entries, historic_entries, thread_failures=None):
    """A FakeLodgify that only reveals `historic_entries` under stayFilter=All.

    `FakeLodgify`'s own handler ignores `stayFilter` entirely -- everything in
    `bookings` answers every filter alike -- so a booking added through it can
    never be genuinely persisted-only: it is always visible to the live
    Current+Upcoming scan too. This subclass reproduces the one distinction
    the whole feature exists for: Current/Upcoming enumerate only
    `live_entries`, while All additionally reaches `historic_entries`. Threads
    are registered for both sets, so a historic booking is resolvable and
    readable once `summarise_refs`'s archive scan reaches it -- exactly what
    `_enrich` depends on. Modelled on `StayFilterFake` in `tests/test_inbox.py`.
    """
    live_bookings, threads = [], {}

    for booking_id, uid, at, sender in live_entries:
        live_bookings.append(booking(booking_id, uid))
        threads[uid] = thread(
            uid, [message(f"m-{uid}", sender, "Invented fixture text.", at)]
        )

    historic_bookings = []

    for booking_id, uid, at, sender in historic_entries:
        historic_bookings.append(booking(booking_id, uid))
        threads[uid] = thread(
            uid, [message(f"m-{uid}", sender, "Invented fixture text.", at)]
        )

    all_bookings = live_bookings + historic_bookings

    class StayFilterFake(FakeLodgify):
        def handler(self, request):
            if request.url.path == "/v2/reservations/bookings":
                asked = request.url.params.get("stayFilter")
                self.bookings = all_bookings if asked == "All" else live_bookings

            return super().handler(request)

    return StayFilterFake(
        bookings=live_bookings,
        threads=threads,
        thread_failures=thread_failures,
    )


def remember(store, ref, at, status=ConversationStatus.NEEDS_ATTENTION.value):
    store.upsert(
        conversation_ref=ref,
        conversation_fingerprint=f"fp-{ref}",
        status=status,
        last_message_at=at,
        last_message_sender="Renter",
        message_count=2,
        property_slug="renovated-2nd-floor-home",
        source="BookingCom",
        booking_status="Booked",
    )


def refs(rows):
    return [row["conversation_ref"] for row in rows]


def test_a_persisted_historic_conversation_appears_in_the_inbox(database):
    """The whole point: a webhook-known conversation the live scan cannot see."""
    fake = live_fake([(1001, "thread-a", "2026-08-01T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")

    rows = build_inbox(fake.inbox(), store).conversations

    assert refs(rows)[0] == "PH-HISTORIC1"


def test_live_and_persisted_rows_merge(database):
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(1001), "2026-09-02T09:00:00")
    remember(store, "PH-HISTORIC1", "2026-09-01T09:00:00")

    rows = build_inbox(fake.inbox(), store).conversations

    assert set(refs(rows)) == {conversation_ref_for(1001), "PH-HISTORIC1"}


def test_a_conversation_in_both_sources_appears_once(database):
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(1001), "2026-01-01T00:00:00")

    rows = build_inbox(fake.inbox(), store).conversations

    listed = refs(rows)

    assert len(listed) == len(set(listed))
    assert listed.count(conversation_ref_for(1001)) == 1


def test_the_live_row_wins_over_a_persisted_duplicate(database):
    """The live read is authoritative, and it is read exactly once.

    Asserting the live values alone proves nothing: the live pass upserts the
    index on its way past, so by the time the persisted rows are merged the
    duplicate already holds the live values. Two things make this bite.

    The thread is scripted to answer differently the second time -- the queued
    payload once, then a stale fallback -- so a row rebuilt from the index and
    re-read is visibly not the live one. And the request counts pin the cost:
    a conversation already read live is never resolved against the archive
    again, which is exactly what the dedupe guard buys.
    """
    live = message("m-live", "Owner", "Invented fixture text.", "2026-09-02T09:00:00")
    stale = message("m-stale", "Renter", "Stale fixture text.", "2026-01-01T00:00:00")

    fake = FakeLodgify(
        bookings=[booking(1001, "thread-a")],
        thread_sequence={"thread-a": [thread("thread-a", [live])]},
        threads={"thread-a": thread("thread-a", [stale])},
    )
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(1001), "2026-01-01T00:00:00")

    rows = build_inbox(fake.inbox(), store).conversations

    assert refs(rows) == [conversation_ref_for(1001)]

    row = rows[0]

    assert row["last_message_at"] == "2026-09-02T09:00:00"
    assert row["last_message_sender"] == "Owner"
    assert row["last_message_excerpt"] == "Invented fixture text."
    assert row["message_count"] == 1
    assert row["preview_unavailable"] is False

    # Nothing from the snapshot survived into the row.
    assert "Stale fixture text." not in str(row.values())
    assert row["last_message_at"] != "2026-01-01T00:00:00"

    # The live row was never treated as persisted-only, so enrichment had
    # nothing to do: no second archive scan, no second thread read.
    assert len(fake.thread_reads) == 1
    assert len(fake.booking_reads) == len(INBOX_STAY_FILTERS)


def test_ordering_is_by_last_message_at_across_both_sources(database):
    fake = live_fake(
        [
            (1001, "thread-a", "2026-09-02T09:00:00", "Owner"),
            (1002, "thread-b", "2026-08-20T09:00:00", "Owner"),
        ]
    )
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(1001), "2026-09-02T09:00:00")
    remember(store, conversation_ref_for(1002), "2026-08-20T09:00:00")
    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")
    remember(store, "PH-HISTORIC2", "2026-08-25T09:00:00")

    assert refs(build_inbox(fake.inbox(), store).conversations) == [
        "PH-HISTORIC1",
        conversation_ref_for(1001),
        "PH-HISTORIC2",
        conversation_ref_for(1002),
    ]


def test_the_limit_is_applied_after_the_merge(database):
    fake = live_fake([(1001, "thread-a", "2026-08-20T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")

    rows = build_inbox(fake.inbox(), store, limit=1).conversations

    assert refs(rows) == ["PH-HISTORIC1"]


def test_a_persisted_only_row_is_enriched_with_a_live_excerpt(database):
    """It survived onto the page, so we read it once to supply a preview.

    Booking 9001 sits in `historic_entries` only, so it is invisible to the
    live Current+Upcoming scan -- genuinely persisted-only, not merely a live
    row that happens to also be remembered. The excerpt this test asserts can
    only come from `_enrich` reading the thread via `summarise_refs`.
    """
    fake = historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_entries=[(9001, "thread-hist", "2026-09-03T12:06:33", "Renter")],
    )
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(9001), "2026-09-03T12:06:33")

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["conversation_ref"] == conversation_ref_for(9001)
    assert row["last_message_excerpt"] == "Invented fixture text."
    assert row["preview_unavailable"] is False


def test_a_failed_enrichment_keeps_the_row_and_invents_nothing(database):
    """Fail closed to visible-with-less-detail, never to absent."""
    fake = live_fake([(1001, "thread-a", "2026-08-01T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    # No booking matches this ref, so enrichment cannot resolve it.
    remember(store, "PH-UNREACHBL", "2026-09-03T12:06:33")

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["conversation_ref"] == "PH-UNREACHBL"
    assert row["last_message_at"] == "2026-09-03T12:06:33"
    assert row["last_message_excerpt"] is None
    assert row["preview_unavailable"] is True


def test_enrichment_pages_the_archive_once_for_the_whole_page(database):
    """Historic rows must not multiply the archive paging cost.

    Three genuinely resolvable, genuinely persisted-only rows (`historic_fake`
    keeps them out of the live Current+Upcoming scan, visible only under
    stayFilter=All). This is the regression guard for a real HTTP 429
    incident: it must fail if `_enrich` is ever changed to resolve refs one
    ref at a time instead of one shared `summarise_refs` call, which is
    exactly what a one-at-a-time sabotage (see the fix-round-1 report) does
    to it.
    """
    fake = historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_entries=[
            (9001, "thread-h1", "2026-09-03T10:00:00", "Renter"),
            (9002, "thread-h2", "2026-09-03T11:00:00", "Renter"),
            (9003, "thread-h3", "2026-09-03T12:00:00", "Renter"),
        ],
    )
    store = ConversationActivityStore(database=database)

    historic_refs = {
        conversation_ref_for(booking_id) for booking_id in (9001, 9002, 9003)
    }

    for booking_id, at in ((9001, "10"), (9002, "11"), (9003, "12")):
        remember(store, conversation_ref_for(booking_id), f"2026-09-03T{at}:00:00")

    before = len(fake.booking_reads)

    rows = build_inbox(fake.inbox(), store).conversations

    enriched = [row for row in rows if row["conversation_ref"] in historic_refs]

    assert len(enriched) == 3
    assert all(row["preview_unavailable"] is False for row in enriched)
    assert all(
        row["last_message_excerpt"] == "Invented fixture text." for row in enriched
    )

    # The live listing scans once per stay filter; enrichment adds exactly one
    # more archive scan for all three persisted-only rows together -- the
    # count must not grow with how many resolvable rows were pending.
    from app.connectors.lodgify.messaging_client import INBOX_STAY_FILTERS

    assert len(fake.booking_reads) - before == len(INBOX_STAY_FILTERS) + 1


def test_enrichment_updates_the_index_when_it_finds_newer_activity(database):
    """The stored row is stale; only a genuine enrichment read can refresh it.

    Booking 9001 is historic-only (invisible to the live scan), so the
    9001 -> 2026-09-03T12:06:33 result can only have come from `_enrich`
    reading the thread, never from the live-scan `_remember` pass.
    """
    fake = historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_entries=[(9001, "thread-hist", "2026-09-03T12:06:33", "Renter")],
    )
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(9001), "2026-09-01T00:00:00")

    build_inbox(fake.inbox(), store)

    assert store.for_conversation(conversation_ref_for(9001)).last_message_at == (
        "2026-09-03T12:06:33"
    )


def test_reading_a_page_row_refreshes_its_index_entry(database):
    """One durable index regardless of trigger.

    Refreshing an existing row is not seeding: the row was already there, and
    the thread was read because it made the page. What a read must never do is
    *create* a row -- see below.
    """
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(1001), "2026-01-01T00:00:00")

    build_inbox(fake.inbox(), store)

    stored = store.for_conversation(conversation_ref_for(1001))

    assert stored is not None
    assert stored.last_message_at == "2026-09-02T09:00:00"


def test_a_read_never_creates_an_index_row(database):
    """Seeding is the preparation cycle's job, so no read may trigger one."""
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    result = build_inbox(fake.inbox(), store)

    assert result.conversations == []
    assert result.incomplete is True
    assert store.for_conversation(conversation_ref_for(1001)) is None


def test_a_historic_row_survives_ordinary_polling(database):
    """Polling must not evict what only a webhook could have told us."""
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")

    build_inbox(fake.inbox(), store)
    build_inbox(fake.inbox(), store)

    assert store.for_conversation("PH-HISTORIC1") is not None
    assert "PH-HISTORIC1" in refs(build_inbox(fake.inbox(), store).conversations)


# -- enrichment failure must preserve, never overwrite ----------------------
#
# The bug these cover: `summarise_refs` used to answer a failed thread read
# with the fail-closed UNKNOWN placeholder, and `_enrich` wrote it over the
# page row *and* back into the activity index. A single 429 replaced a
# webhook-recorded Historic conversation's real timestamp with null, sinking it
# to the bottom of the Inbox -- permanently, because the live Current+Upcoming
# scan never enumerates a Historic stay and so can never repair it.

HISTORIC_REF = conversation_ref_for(9001)

HISTORIC_AT = "2026-09-03T12:06:33"


def failing_historic_fake(failure, at=HISTORIC_AT):
    """A page with one live row and one unreadable persisted-only row."""
    return historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_entries=[(9001, "thread-hist", at, "Renter")],
        thread_failures={"thread-hist": failure},
    )


def stored_historic(store):
    return store.for_conversation(HISTORIC_REF)


def test_a_rate_limited_enrichment_keeps_the_persisted_timestamp(database):
    fake = failing_historic_fake(429)
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["conversation_ref"] == HISTORIC_REF
    assert row["last_message_at"] == HISTORIC_AT


def test_a_timed_out_enrichment_keeps_the_persisted_timestamp(database):
    import httpx

    fake = failing_historic_fake(httpx.ReadTimeout("scripted timeout"))
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["conversation_ref"] == HISTORIC_REF
    assert row["last_message_at"] == HISTORIC_AT


def test_a_failed_enrichment_preserves_every_persisted_field(database):
    fake = failing_historic_fake(429)
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["status"] == ConversationStatus.NEEDS_ATTENTION.value
    assert row["last_message_sender"] == "Renter"
    assert row["message_count"] == 2
    assert row["fingerprint"] == f"fp-{HISTORIC_REF}"
    assert row["property_slug"] == "renovated-2nd-floor-home"
    assert row["source"] == "BookingCom"
    assert row["booking_status"] == "Booked"


def test_a_failed_enrichment_shows_no_preview_and_invents_no_excerpt(database):
    fake = failing_historic_fake(429)
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["preview_unavailable"] is True
    assert row["last_message_excerpt"] is None


def test_a_failed_enrichment_writes_nothing_back_to_the_index(database):
    """The index is the only record this conversation moved. Do not touch it."""
    fake = failing_historic_fake(429)
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    before = stored_historic(store)

    build_inbox(fake.inbox(), store)

    after = stored_historic(store)

    assert after.last_message_at == HISTORIC_AT
    assert after.last_message_sender == "Renter"
    assert after.message_count == 2
    assert after.conversation_fingerprint == f"fp-{HISTORIC_REF}"
    assert after.status == ConversationStatus.NEEDS_ATTENTION.value
    # Not merely equal in value: the row was never rewritten at all.
    assert after.last_refreshed_at == before.last_refreshed_at


def test_a_failed_enrichment_keeps_its_ordering_position(database):
    """Ordering uses the persisted timestamp, so a failure cannot move a row.

    Ordering happens before enrichment, so the damage never showed on the call
    that failed -- it showed on the *next* one, reading back the nulls the
    failed call had written. Hence two builds: the second is the one that
    would have found the row at the bottom of the page.
    """
    inbox = historic_fake(
        live_entries=[
            (1001, "thread-a", "2026-09-02T09:00:00", "Owner"),
            (1002, "thread-b", "2026-08-01T09:00:00", "Owner"),
        ],
        historic_entries=[(9001, "thread-hist", HISTORIC_AT, "Renter")],
        thread_failures={"thread-hist": 429},
    ).inbox()
    store = ConversationActivityStore(database=database)

    # Newer than 1001, older than nothing: the top of the page.
    remember(store, HISTORIC_REF, HISTORIC_AT)
    remember(store, conversation_ref_for(1001), "2026-09-02T09:00:00")
    remember(store, conversation_ref_for(1002), "2026-08-01T09:00:00")

    expected = [
        HISTORIC_REF,
        conversation_ref_for(1001),
        conversation_ref_for(1002),
    ]

    assert refs(build_inbox(inbox, store).conversations) == expected
    assert refs(build_inbox(inbox, store).conversations) == expected


def test_a_later_successful_enrichment_repairs_the_row(database):
    """Preserving is not freezing: the next good read still updates everything."""
    fake = failing_historic_fake(429, at="2026-09-04T08:00:00")
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    failed = build_inbox(fake.inbox(), store).conversations[0]

    assert failed["last_message_at"] == HISTORIC_AT
    assert failed["preview_unavailable"] is True

    # The provider recovers.
    fake.thread_failures = {}

    repaired = build_inbox(fake.inbox(), store).conversations[0]

    assert repaired["conversation_ref"] == HISTORIC_REF
    assert repaired["last_message_at"] == "2026-09-04T08:00:00"
    assert repaired["last_message_excerpt"] == "Invented fixture text."
    assert repaired["preview_unavailable"] is False
    assert stored_historic(store).last_message_at == "2026-09-04T08:00:00"


def test_one_failed_enrichment_does_not_damage_the_other_rows(database):
    """Per-row failure, not per-page."""
    fake = historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_entries=[
            (9001, "thread-hist", HISTORIC_AT, "Renter"),
            (9002, "thread-ok", "2026-09-03T11:00:00", "Renter"),
        ],
        thread_failures={"thread-hist": 429},
    )
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)
    remember(store, conversation_ref_for(9002), "2026-09-03T11:00:00")

    rows = {
        row["conversation_ref"]: row
        for row in build_inbox(fake.inbox(), store).conversations
    }

    healthy = rows[conversation_ref_for(9002)]

    assert healthy["last_message_excerpt"] == "Invented fixture text."
    assert healthy["preview_unavailable"] is False

    broken = rows[HISTORIC_REF]

    assert broken["last_message_at"] == HISTORIC_AT
    assert broken["preview_unavailable"] is True


def test_a_webhook_recorded_row_survives_repeated_provider_failures(database):
    """A transient outage must not durably sink a conversation."""
    fake = failing_historic_fake(429)
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    for _ in range(3):
        rows = build_inbox(fake.inbox(), store).conversations

        assert refs(rows)[0] == HISTORIC_REF
        assert rows[0]["last_message_at"] == HISTORIC_AT

    assert stored_historic(store).last_message_at == HISTORIC_AT


def test_an_omitted_ref_leaves_the_index_untouched(database, monkeypatch):
    """The contract stated as a test: absence means preserve.

    `summarise_refs` is forced to answer the way a failed read now answers --
    by omitting the ref entirely -- regardless of what the provider would have
    done. Nothing downstream may read that as "no messages".
    """
    fake = historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_entries=[(9001, "thread-hist", "2026-09-09T09:00:00", "Renter")],
    )
    inbox = fake.inbox()
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    monkeypatch.setattr(inbox, "summarise_refs", lambda pending: {})

    row = build_inbox(inbox, store).conversations[0]

    assert row["conversation_ref"] == HISTORIC_REF
    assert row["last_message_at"] == HISTORIC_AT
    assert row["last_message_excerpt"] is None
    assert row["preview_unavailable"] is True

    stored = stored_historic(store)

    assert stored.last_message_at == HISTORIC_AT
    assert stored.message_count == 2
    assert stored.conversation_fingerprint == f"fp-{HISTORIC_REF}"


# -- a SUCCESSFUL empty read must not overwrite known-good persisted activity
#
# The bug: `summarise_refs` never treats a genuinely empty thread as a
# failure -- 42 threads in the real account return an empty `messages` array
# while still advertising a `last_message_date`, so an empty read is a real,
# *present* observation, not an absence. Without a guard, `_enrich` trusted it
# as authoritative and overwrote a known-good persisted row with nulls,
# sinking a Historic conversation exactly as permanently as the earlier
# failure bug did -- the live scan can never re-enumerate it to repair the
# damage. This is a different bug from the one above: `summarise_refs` must
# keep answering with the empty summary present in its mapping (that is
# correct -- the read genuinely succeeded), so the guard has to live in
# `_enrich` itself, comparing what came back against what was already known.

EMPTY_REF = conversation_ref_for(9002)


def empty_historic_fake(live_entries, historic_booking_id, historic_uid, at):
    """A page with one live row and one persisted-only row whose thread reads
    back with zero messages -- a successful read, not a `thread_failures`
    entry. Built from `historic_fake` and then the scripted thread is
    replaced with a genuinely empty one; the booking itself is untouched, so
    the ref stays resolvable via the archive scan.
    """
    fake = historic_fake(
        live_entries=live_entries,
        historic_entries=[(historic_booking_id, historic_uid, at, "Renter")],
    )
    fake.threads[historic_uid] = thread(historic_uid, [])

    return fake


def test_a_persisted_row_with_no_known_good_activity_keeps_a_legitimate_empty_read(
    database,
):
    """Case 1: nothing to lose, so the empty read is trusted as-is.

    This is today's behaviour, unguarded: the persisted row never had
    known-good activity, so there is nothing for the guard to protect and the
    empty summary is used and stored exactly as it always was.
    """
    fake = empty_historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_booking_id=9002,
        historic_uid="thread-empty",
        at="2026-09-03T12:06:33",
    )
    store = ConversationActivityStore(database=database)

    store.upsert(
        conversation_ref=EMPTY_REF,
        conversation_fingerprint="",
        status=ConversationStatus.UNKNOWN.value,
        last_message_at=None,
        last_message_sender=None,
        message_count=0,
        property_slug="renovated-2nd-floor-home",
        source="BookingCom",
        booking_status="Booked",
    )

    rows = {
        row["conversation_ref"]: row
        for row in build_inbox(fake.inbox(), store).conversations
    }
    row = rows[EMPTY_REF]

    assert row["last_message_at"] is None
    assert row["message_count"] == 0
    assert row["preview_unavailable"] is False
    assert row["last_message_excerpt"] is None

    # The empty read was trusted, so it was written back to the index too --
    # unlike case 2, there was no known-good activity for a write to destroy.
    stored = store.for_conversation(EMPTY_REF)

    assert stored.last_message_at is None
    assert stored.message_count == 0


def test_known_good_activity_survives_a_successful_empty_read(database):
    """Case 2: the guard itself. A present-but-empty read must not sink a
    persisted row that already recorded real activity."""
    fake = empty_historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_booking_id=9002,
        historic_uid="thread-empty",
        at=HISTORIC_AT,
    )
    store = ConversationActivityStore(database=database)

    remember(store, EMPTY_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["conversation_ref"] == EMPTY_REF
    assert row["last_message_at"] == HISTORIC_AT
    assert row["last_message_sender"] == "Renter"
    assert row["status"] == ConversationStatus.NEEDS_ATTENTION.value
    assert row["message_count"] == 2
    assert row["fingerprint"] == f"fp-{EMPTY_REF}"
    assert row["last_message_excerpt"] is None
    assert row["preview_unavailable"] is True

    stored_before = store.for_conversation(EMPTY_REF)

    build_inbox(fake.inbox(), store)

    stored_after = store.for_conversation(EMPTY_REF)

    # Not merely equal in value: nothing was ever written back.
    assert stored_after.last_message_at == HISTORIC_AT
    assert stored_after.message_count == 2
    assert stored_after.conversation_fingerprint == f"fp-{EMPTY_REF}"
    assert stored_after.status == ConversationStatus.NEEDS_ATTENTION.value
    assert stored_after.last_refreshed_at == stored_before.last_refreshed_at


def test_a_known_message_count_is_not_reset_to_zero_by_an_empty_enrichment(database):
    """Explicit guard against the exact regression this fix targets."""
    fake = empty_historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_booking_id=9002,
        historic_uid="thread-empty",
        at=HISTORIC_AT,
    )
    store = ConversationActivityStore(database=database)

    remember(store, EMPTY_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["message_count"] == 2
    assert store.for_conversation(EMPTY_REF).message_count == 2


def test_a_successful_non_empty_read_still_overrides_known_good_activity(database):
    """Case 3: unchanged from before this guard -- live wins when it is live."""
    fake = historic_fake(
        live_entries=[(1001, "thread-a", "2026-08-01T09:00:00", "Owner")],
        historic_entries=[(9002, "thread-fresh", "2026-09-04T08:00:00", "Renter")],
    )
    store = ConversationActivityStore(database=database)

    remember(store, EMPTY_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["conversation_ref"] == EMPTY_REF
    assert row["last_message_at"] == "2026-09-04T08:00:00"
    assert row["last_message_excerpt"] == "Invented fixture text."
    assert row["preview_unavailable"] is False

    stored = store.for_conversation(EMPTY_REF)

    assert stored.last_message_at == "2026-09-04T08:00:00"


def test_a_failed_read_still_preserves_known_good_activity_alongside_the_empty_read_guard(
    database,
):
    """Case 4: confirms the older failure-path guard has not regressed."""
    fake = failing_historic_fake(429)
    store = ConversationActivityStore(database=database)

    remember(store, HISTORIC_REF, HISTORIC_AT)

    row = build_inbox(fake.inbox(), store).conversations[0]

    assert row["conversation_ref"] == HISTORIC_REF
    assert row["last_message_at"] == HISTORIC_AT
    assert row["preview_unavailable"] is True

    assert store.for_conversation(HISTORIC_REF).last_message_at == HISTORIC_AT


def test_a_row_preserved_by_the_empty_read_guard_keeps_its_ordering_position(database):
    """Ordering happens before enrichment, so corruption would only show up on
    the *next* build -- the same two-call technique used for the failure-path
    guard above.
    """
    fake = empty_historic_fake(
        live_entries=[
            (1001, "thread-a", "2026-09-02T09:00:00", "Owner"),
            (1002, "thread-b", "2026-08-01T09:00:00", "Owner"),
        ],
        historic_booking_id=9002,
        historic_uid="thread-empty",
        at=HISTORIC_AT,
    )
    store = ConversationActivityStore(database=database)

    # Newer than 1001, older than nothing: the top of the page.
    remember(store, EMPTY_REF, HISTORIC_AT)
    remember(store, conversation_ref_for(1001), "2026-09-02T09:00:00")
    remember(store, conversation_ref_for(1002), "2026-08-01T09:00:00")

    expected = [
        EMPTY_REF,
        conversation_ref_for(1001),
        conversation_ref_for(1002),
    ]

    assert refs(build_inbox(fake.inbox(), store).conversations) == expected
    assert refs(build_inbox(fake.inbox(), store).conversations) == expected
