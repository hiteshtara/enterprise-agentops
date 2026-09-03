"""Ordering the Inbox from the persisted index instead of from a live scan.

`GET /inbox` used to read a thread for every current-and-upcoming conversation
-- ~155 provider requests against the live account -- for one reason: to learn
each conversation's last-message time, which is what the Inbox orders by. At a
poll interval that was caught live returning HTTP 429 on the first booking
page, which fails the whole request.

Request counts are therefore the point of this file, and they are asserted
rather than assumed. Against the live account the booking scan is ~3 pages;
in these fixtures every stay filter fits on one page, so the same scan is
`len(INBOX_STAY_FILTERS)` pages. The shape is what matters: the scan does not
grow with the number of conversations, and thread reads do not grow past the
page size or the batch size.

Everything runs through `httpx.MockTransport`. No socket, no credential, no
real guest data, nothing sends.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.connectors.lodgify.inbox import BOOKING_SCAN_SIZE
from app.connectors.lodgify.messaging_client import INBOX_STAY_FILTERS
from app.connectors.lodgify.messaging_models import ConversationStatus
from app.connectors.lodgify.refs import conversation_ref_for
from app.conversation_activity import ConversationActivityStore
from app.inbox_view import (
    SEED_BATCH,
    STALE_ACTIVITY_THRESHOLD_SECONDS,
    SWEEP_SIZE,
    build_inbox,
    discover_conversations,
    prepare_activity_index,
)
from tests.lodgify_fakes import FakeLodgify, booking, message, thread

GUEST_TEXT = "Invented fixture question from a guest."

BOOKING_SCAN_PAGES = len(INBOX_STAY_FILTERS)


# -- fixtures, all invented -------------------------------------------------


def uid_for(booking_id: int) -> str:
    return f"thread-{booking_id}"


def account(
    live_ids: list[int],
    historic_ids: list[int] | None = None,
    at: str = "2026-09-01T09:00:00",
    **kwargs,
) -> FakeLodgify:
    """A scripted account whose Historic bookings answer only `stayFilter=All`.

    `FakeLodgify`'s handler ignores `stayFilter`, so without this a booking is
    always visible to the Current+Upcoming scan and can never be genuinely
    index-only. Modelled on `historic_fake` in `tests/test_inbox_view.py`.
    """
    historic_ids = historic_ids or []

    threads = {}
    live_rows = []
    historic_rows = []

    for booking_id in live_ids + historic_ids:
        uid = uid_for(booking_id)
        threads[uid] = thread(
            uid,
            [message(f"m-{booking_id}", "Renter", GUEST_TEXT, at)],
        )

    for booking_id in live_ids:
        live_rows.append(booking(booking_id, uid_for(booking_id)))

    for booking_id in historic_ids:
        historic_rows.append(booking(booking_id, uid_for(booking_id)))

    every_row = live_rows + historic_rows

    class StayFilterFake(FakeLodgify):
        def handler(self, request):
            if request.url.path == "/v2/reservations/bookings":
                asked = request.url.params.get("stayFilter")
                self.bookings = every_row if asked == "All" else live_rows

            return super().handler(request)

    return StayFilterFake(bookings=live_rows, threads=threads, **kwargs)


def remember(
    store: ConversationActivityStore,
    ref: str,
    at: str | None,
    status: str = ConversationStatus.NEEDS_ATTENTION.value,
    message_count: int = 2,
    property_slug: str = "renovated-2nd-floor-home",
) -> None:
    store.upsert(
        conversation_ref=ref,
        conversation_fingerprint=f"fp-{ref}",
        status=status,
        last_message_at=at,
        last_message_sender="Renter",
        message_count=message_count,
        property_slug=property_slug,
        source="BookingCom",
        booking_status="Booked",
    )


def backdate(store: ConversationActivityStore, ref: str, seconds: float) -> None:
    """Move one row's `last_refreshed_at` into the past.

    The store always stamps `datetime.now(UTC)`, so a test that needs an old
    row edits the column directly rather than sleeping.
    """
    from sqlalchemy import select

    from app.db_models import ConversationActivityRecord

    when = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()

    with store.database.session() as session:
        record = session.scalar(
            select(ConversationActivityRecord).where(
                ConversationActivityRecord.conversation_ref == ref
            )
        )
        record.last_refreshed_at = when
        session.commit()


def refs(rows) -> list[str]:
    return [row["conversation_ref"] for row in rows]


@pytest.fixture
def store(database) -> ConversationActivityStore:
    return ConversationActivityStore(database=database)


# -- A. bounded, ordered store reads ----------------------------------------


def test_ordered_activity_is_newest_first(store):
    remember(store, "PH-AAAAAAAA", "2026-09-01T09:00:00")
    remember(store, "PH-BBBBBBBB", "2026-09-03T09:00:00")
    remember(store, "PH-CCCCCCCC", "2026-09-02T09:00:00")

    ordered = [row.conversation_ref for row in store.ordered_activity(10)]

    assert ordered == ["PH-BBBBBBBB", "PH-CCCCCCCC", "PH-AAAAAAAA"]


def test_ordered_activity_breaks_ties_on_the_reference_ascending(store):
    remember(store, "PH-CCCCCCCC", "2026-09-01T09:00:00")
    remember(store, "PH-AAAAAAAA", "2026-09-01T09:00:00")
    remember(store, "PH-BBBBBBBB", "2026-09-01T09:00:00")

    ordered = [row.conversation_ref for row in store.ordered_activity(10)]

    assert ordered == ["PH-AAAAAAAA", "PH-BBBBBBBB", "PH-CCCCCCCC"]


def test_ordered_activity_sorts_a_row_with_no_timestamp_last(store):
    remember(store, "PH-AAAAAAAA", None)
    remember(store, "PH-BBBBBBBB", "2026-01-01T09:00:00")

    ordered = [row.conversation_ref for row in store.ordered_activity(10)]

    assert ordered == ["PH-BBBBBBBB", "PH-AAAAAAAA"]


def test_ordered_activity_applies_the_limit(store):
    for index in range(10):
        remember(store, f"PH-{index:08d}", f"2026-09-{index + 1:02d}T09:00:00")

    assert len(store.ordered_activity(3)) == 3


def test_ordered_activity_can_be_narrowed_to_one_property(store):
    remember(store, "PH-AAAAAAAA", "2026-09-03T09:00:00", property_slug="one")
    remember(store, "PH-BBBBBBBB", "2026-09-02T09:00:00", property_slug="two")

    ordered = store.ordered_activity(10, property_slug="two")

    assert [row.conversation_ref for row in ordered] == ["PH-BBBBBBBB"]


def test_least_recently_refreshed_returns_the_oldest_first(store):
    remember(store, "PH-AAAAAAAA", "2026-09-01T09:00:00")
    remember(store, "PH-BBBBBBBB", "2026-09-01T09:00:00")
    remember(store, "PH-CCCCCCCC", "2026-09-01T09:00:00")

    backdate(store, "PH-BBBBBBBB", 3600)
    backdate(store, "PH-CCCCCCCC", 60)

    ordered = [row.conversation_ref for row in store.least_recently_refreshed(2)]

    assert ordered == ["PH-BBBBBBBB", "PH-CCCCCCCC"]


def test_least_recently_refreshed_can_exclude_refs(store):
    remember(store, "PH-AAAAAAAA", "2026-09-01T09:00:00")
    remember(store, "PH-BBBBBBBB", "2026-09-01T09:00:00")

    backdate(store, "PH-AAAAAAAA", 3600)

    ordered = store.least_recently_refreshed(5, exclude=("PH-AAAAAAAA",))

    assert [row.conversation_ref for row in ordered] == ["PH-BBBBBBBB"]


def test_known_refs_returns_only_the_refs_that_have_a_row(store):
    remember(store, "PH-AAAAAAAA", "2026-09-01T09:00:00")

    assert store.known_refs(["PH-AAAAAAAA", "PH-BBBBBBBB"]) == {"PH-AAAAAAAA"}


def test_known_refs_of_nothing_is_empty(store):
    assert store.known_refs([]) == set()


def test_oldest_refreshed_at_is_the_minimum_of_the_named_rows(store):
    remember(store, "PH-AAAAAAAA", "2026-09-01T09:00:00")
    remember(store, "PH-BBBBBBBB", "2026-09-01T09:00:00")

    backdate(store, "PH-AAAAAAAA", 7200)

    oldest = store.oldest_refreshed_at(["PH-AAAAAAAA", "PH-BBBBBBBB"])
    only_b = store.oldest_refreshed_at(["PH-BBBBBBBB"])

    assert oldest is not None
    assert only_b is not None
    assert oldest < only_b


def test_oldest_refreshed_at_of_unknown_refs_is_none(store):
    assert store.oldest_refreshed_at(["PH-NOTHERE1"]) is None


# -- B. GET /inbox: ordering costs nothing ----------------------------------


def test_a_normal_page_costs_the_booking_scan_plus_one_read_per_row(store):
    """The number this change exists to produce.

    Thirty current conversations, a page of twenty: the booking scan does not
    grow with the account, and thread reads are bounded by the page, not by the
    number of conversations.
    """
    live = list(range(1001, 1031))
    fake = account(live)

    for index, booking_id in enumerate(live):
        remember(
            store, conversation_ref_for(booking_id), f"2026-09-{index + 1:02d}T09:00:00"
        )

    result = build_inbox(fake.inbox(), store, limit=20)

    assert len(result.conversations) == 20
    assert len(fake.booking_reads) == BOOKING_SCAN_PAGES
    assert len(fake.thread_reads) == 20


def test_ordering_costs_zero_thread_reads(store):
    """A page of one, out of thirty indexed conversations.

    If ordering still came from a live scan this would read thirty threads to
    decide which one to show. It reads exactly the one it shows.
    """
    live = list(range(1001, 1031))
    fake = account(live)

    for index, booking_id in enumerate(live):
        remember(
            store, conversation_ref_for(booking_id), f"2026-09-{index + 1:02d}T09:00:00"
        )

    result = build_inbox(fake.inbox(), store, limit=1)

    assert refs(result.conversations) == [conversation_ref_for(1030)]
    assert len(fake.thread_reads) == 1
    assert len(fake.booking_reads) == BOOKING_SCAN_PAGES


def test_ordering_comes_from_the_index_not_from_the_provider(store):
    """The index disagrees with the threads on purpose.

    Every thread carries the same timestamp, so a live-scan ordering would fall
    back to the reference tie-break. The order asserted here can only come from
    the persisted `last_message_at`.
    """
    fake = account([1001, 1002, 1003])

    remember(store, conversation_ref_for(1001), "2026-01-01T09:00:00")
    remember(store, conversation_ref_for(1002), "2026-09-09T09:00:00")
    remember(store, conversation_ref_for(1003), "2026-05-05T09:00:00")

    result = build_inbox(fake.inbox(), store, limit=20)

    assert refs(result.conversations) == [
        conversation_ref_for(1002),
        conversation_ref_for(1003),
        conversation_ref_for(1001),
    ]


def test_only_the_rows_on_the_page_are_re_read(store):
    live = list(range(1001, 1011))
    fake = account(live)

    for index, booking_id in enumerate(live):
        remember(
            store, conversation_ref_for(booking_id), f"2026-09-{index + 1:02d}T09:00:00"
        )

    build_inbox(fake.inbox(), store, limit=3)

    read = {request.url.path.rsplit("/", 1)[-1] for request in fake.thread_reads}

    assert read == {uid_for(1010), uid_for(1009), uid_for(1008)}


def test_a_historic_row_costs_exactly_one_shared_archive_scan(store):
    """Two index-only rows, one archive scan -- not one per row.

    The regression guard for a real HTTP 429 incident. The count must not grow
    with how many Historic rows made the page.
    """
    fake = account([1001], historic_ids=[9001, 9002])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")
    remember(store, conversation_ref_for(9001), "2026-09-02T09:00:00")
    remember(store, conversation_ref_for(9002), "2026-09-03T09:00:00")

    result = build_inbox(fake.inbox(), store, limit=20)

    assert len(result.conversations) == 3
    assert all(row["preview_unavailable"] is False for row in result.conversations)

    # One Current+Upcoming scan for resolution, plus exactly one `All` scan
    # shared by both Historic rows.
    assert len(fake.booking_reads) == BOOKING_SCAN_PAGES + 1


# -- C. GET /inbox never seeds ----------------------------------------------


def test_an_empty_index_renders_nothing_and_reads_no_threads(store):
    """Seeding is the preparation cycle's job. A read may never trigger one."""
    fake = account(list(range(1001, 1031)))

    result = build_inbox(fake.inbox(), store, limit=20)

    assert result.conversations == []
    assert len(fake.thread_reads) == 0
    assert len(fake.booking_reads) == BOOKING_SCAN_PAGES


def test_an_unseeded_ref_is_never_written_by_a_read(store):
    fake = account([1001])

    build_inbox(fake.inbox(), store, limit=20)
    build_inbox(fake.inbox(), store, limit=20)

    assert store.for_conversation(conversation_ref_for(1001)) is None


def test_an_unseeded_ref_marks_the_page_incomplete(store):
    """The list may genuinely be short, and the Inbox says so."""
    fake = account([1001, 1002])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")

    result = build_inbox(fake.inbox(), store, limit=20)

    assert refs(result.conversations) == [conversation_ref_for(1001)]
    assert result.incomplete is True


def test_a_fully_seeded_account_is_not_incomplete(store):
    fake = account([1001, 1002])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")
    remember(store, conversation_ref_for(1002), "2026-09-02T09:00:00")

    assert build_inbox(fake.inbox(), store, limit=20).incomplete is False


# -- D. staleness -----------------------------------------------------------


def test_a_page_past_the_threshold_is_flagged_stale(store):
    fake = account([1001])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")
    backdate(store, conversation_ref_for(1001), STALE_ACTIVITY_THRESHOLD_SECONDS + 60)

    result = build_inbox(fake.inbox(), store, limit=20)

    assert result.activity_stale is True


def test_a_freshly_indexed_page_is_not_stale(store):
    fake = account([1001])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")

    assert build_inbox(fake.inbox(), store, limit=20).activity_stale is False


def test_an_empty_page_is_not_stale(store):
    fake = account([1001])

    assert build_inbox(fake.inbox(), store, limit=20).activity_stale is False


def test_staleness_is_measured_before_the_page_is_re_read(store):
    """Reading the page refreshes its rows, so the flag must be decided first.

    Otherwise every page would report itself fresh the instant it was built,
    and the flag would never fire at all.
    """
    fake = account([1001])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")
    backdate(store, conversation_ref_for(1001), STALE_ACTIVITY_THRESHOLD_SECONDS + 60)

    result = build_inbox(fake.inbox(), store, limit=20)

    assert result.activity_stale is True
    # The read did happen, and did refresh the row.
    assert len(fake.thread_reads) == 1


def test_the_stale_clock_is_injectable(store):
    fake = account([1001])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")

    later = datetime.now(UTC) + timedelta(seconds=STALE_ACTIVITY_THRESHOLD_SECONDS + 60)

    assert build_inbox(fake.inbox(), store, limit=20, now=later).activity_stale is True


# -- E. the preparation cycle: progressive cold start -----------------------


def test_cold_start_seeds_exactly_one_batch(store):
    """Never a thread read per conversation in the account.

    The obvious cold start -- read everything once -- is the same burst that
    was caught live returning 429.
    """
    live = list(range(1001, 1001 + SEED_BATCH * 2))
    fake = account(live)

    cycle = prepare_activity_index(fake.inbox(), store)

    assert cycle.seeded == SEED_BATCH
    assert len(fake.thread_reads) == SEED_BATCH
    assert len(fake.booking_reads) == BOOKING_SCAN_PAGES
    assert len(store.all_activity()) == SEED_BATCH


def test_the_next_cycle_seeds_the_next_batch(store):
    live = list(range(1001, 1001 + SEED_BATCH * 2))
    fake = account(live)

    prepare_activity_index(fake.inbox(), store)

    seeded_first = {row.conversation_ref for row in store.all_activity()}

    before = len(fake.thread_reads)

    cycle = prepare_activity_index(fake.inbox(), store)

    assert cycle.seeded == SEED_BATCH
    assert len(fake.thread_reads) - before == SEED_BATCH
    assert len(store.all_activity()) == SEED_BATCH * 2

    seeded_second = {row.conversation_ref for row in store.all_activity()}

    # The second batch is new work, not the first batch read again.
    assert seeded_first < seeded_second


def test_coverage_completes_and_the_cycle_then_sweeps(store):
    live = list(range(1001, 1001 + SEED_BATCH + 3))
    fake = account(live)

    first = prepare_activity_index(fake.inbox(), store)

    assert first.seeded == SEED_BATCH
    assert first.unseeded_remaining == 3

    second = prepare_activity_index(fake.inbox(), store)

    assert second.seeded == 3
    assert second.unseeded_remaining == 0

    third = prepare_activity_index(fake.inbox(), store)

    # Normal rotating-sweep semantics, bounded by SWEEP_SIZE rather than by
    # how many conversations the account has.
    assert third.seeded == 0
    assert third.swept == SWEEP_SIZE


def test_unseeded_refs_outrank_stale_indexed_refs(store):
    """Re-reading an indexed row while another has never been read at all is
    spending the budget on the less valuable half."""
    live = list(range(1001, 1001 + SEED_BATCH + 5))
    fake = account(live)

    # Five rows already indexed, and deliberately the stalest rows there are.
    already = live[:5]

    for booking_id in already:
        remember(store, conversation_ref_for(booking_id), "2026-01-01T09:00:00")
        backdate(store, conversation_ref_for(booking_id), 86_400)

    before = len(fake.thread_reads)

    cycle = prepare_activity_index(fake.inbox(), store)

    assert cycle.seeded == SEED_BATCH
    assert cycle.swept == 0
    assert len(fake.thread_reads) - before == SEED_BATCH

    read = {request.url.path.rsplit("/", 1)[-1] for request in fake.thread_reads}

    # Not one of the stale-but-indexed rows was touched.
    assert read.isdisjoint({uid_for(booking_id) for booking_id in already})


def test_a_webhook_upserted_ref_is_not_redundantly_seeded(store):
    """A webhook may seed any conversation immediately, and takes precedence."""
    fake = account([1001, 1002])

    remember(store, conversation_ref_for(1001), "2026-09-09T09:00:00")

    cycle = prepare_activity_index(fake.inbox(), store)

    assert cycle.seeded == 1

    read = {request.url.path.rsplit("/", 1)[-1] for request in fake.thread_reads}

    assert read == {uid_for(1002)}
    # The webhook's own record is untouched by the seeding batch.
    assert store.for_conversation(conversation_ref_for(1001)).last_message_at == (
        "2026-09-09T09:00:00"
    )


def test_a_provider_failure_during_warm_up_preserves_progress(store):
    """Whatever was seeded stays seeded, and the page stays visibly incomplete."""
    live = [1001, 1002, 1003]
    fake = account(live, thread_failures={uid_for(1002): 429})

    cycle = prepare_activity_index(fake.inbox(), store)

    assert cycle.seeded == 2
    assert cycle.unseeded_remaining == 1

    indexed = {row.conversation_ref for row in store.all_activity()}

    assert indexed == {conversation_ref_for(1001), conversation_ref_for(1003)}

    result = build_inbox(fake.inbox(), store, limit=20)

    assert result.incomplete is True
    assert len(result.conversations) == 2

    # The provider recovers and the next cycle finishes the job.
    fake.thread_failures = {}

    recovered = prepare_activity_index(fake.inbox(), store)

    assert recovered.seeded == 1
    assert build_inbox(fake.inbox(), store, limit=20).incomplete is False


# -- F. the rotating sweep --------------------------------------------------


def test_the_sweep_costs_the_booking_scan_plus_one_read_per_swept_row(store):
    live = list(range(1001, 1001 + SWEEP_SIZE * 2))
    fake = account(live)

    for booking_id in live:
        remember(store, conversation_ref_for(booking_id), "2026-09-01T09:00:00")

    before_bookings = len(fake.booking_reads)
    before_threads = len(fake.thread_reads)

    cycle = prepare_activity_index(fake.inbox(), store)

    assert cycle.seeded == 0
    assert cycle.swept == SWEEP_SIZE
    assert len(fake.booking_reads) - before_bookings == BOOKING_SCAN_PAGES
    assert len(fake.thread_reads) - before_threads == SWEEP_SIZE


def test_the_sweep_takes_the_least_recently_refreshed_rows(store):
    live = list(range(1001, 1011))
    fake = account(live)

    for booking_id in live:
        remember(store, conversation_ref_for(booking_id), "2026-09-01T09:00:00")

    stalest = live[:3]

    for booking_id in stalest:
        backdate(store, conversation_ref_for(booking_id), 86_400)

    before = len(fake.thread_reads)

    prepare_activity_index(fake.inbox(), store, sweep_size=3)

    read = {request.url.path.rsplit("/", 1)[-1] for request in fake.thread_reads}

    assert len(fake.thread_reads) - before == 3
    assert read == {uid_for(booking_id) for booking_id in stalest}


def test_a_missed_webhook_is_recovered_by_the_sweep(store):
    """The only guarantee this design makes about an unnoticed message.

    The index holds a stale timestamp for a conversation whose thread has since
    moved on, and no webhook arrived to say so. One sweep cycle repairs it.
    """
    fake = account([1001])
    fake.threads[uid_for(1001)] = thread(
        uid_for(1001),
        [message("m-new", "Renter", GUEST_TEXT, "2026-09-30T09:00:00")],
    )

    remember(store, conversation_ref_for(1001), "2026-01-01T09:00:00")

    prepare_activity_index(fake.inbox(), store)

    assert store.for_conversation(conversation_ref_for(1001)).last_message_at == (
        "2026-09-30T09:00:00"
    )


def test_the_sweep_reaches_a_historic_row_through_one_shared_scan(store):
    fake = account([1001], historic_ids=[9001, 9002])

    for booking_id in (1001, 9001, 9002):
        remember(store, conversation_ref_for(booking_id), "2026-01-01T09:00:00")

    before = len(fake.booking_reads)

    prepare_activity_index(fake.inbox(), store)

    assert len(fake.booking_reads) - before == BOOKING_SCAN_PAGES + 1


# -- G. failed and empty reads never erase known-good activity --------------


def test_a_failed_page_read_keeps_the_known_good_row(store):
    fake = account([1001], thread_failures={uid_for(1001): 429})

    remember(store, conversation_ref_for(1001), "2026-09-03T12:06:33")

    before = store.for_conversation(conversation_ref_for(1001))

    row = build_inbox(fake.inbox(), store, limit=20).conversations[0]

    assert row["last_message_at"] == "2026-09-03T12:06:33"
    assert row["message_count"] == 2
    assert row["preview_unavailable"] is True

    after = store.for_conversation(conversation_ref_for(1001))

    assert after.last_message_at == "2026-09-03T12:06:33"
    assert after.last_refreshed_at == before.last_refreshed_at


def test_an_empty_page_read_keeps_the_known_good_row(store):
    fake = account([1001])
    fake.threads[uid_for(1001)] = thread(uid_for(1001), [])

    remember(store, conversation_ref_for(1001), "2026-09-03T12:06:33")

    before = store.for_conversation(conversation_ref_for(1001))

    row = build_inbox(fake.inbox(), store, limit=20).conversations[0]

    assert row["last_message_at"] == "2026-09-03T12:06:33"
    assert row["message_count"] == 2
    assert row["preview_unavailable"] is True

    after = store.for_conversation(conversation_ref_for(1001))

    assert after.last_message_at == "2026-09-03T12:06:33"
    assert after.last_refreshed_at == before.last_refreshed_at


def test_a_failed_sweep_read_keeps_the_known_good_row(store):
    fake = account([1001], thread_failures={uid_for(1001): 429})

    remember(store, conversation_ref_for(1001), "2026-09-03T12:06:33")

    before = store.for_conversation(conversation_ref_for(1001))

    prepare_activity_index(fake.inbox(), store)

    after = store.for_conversation(conversation_ref_for(1001))

    assert after.last_message_at == "2026-09-03T12:06:33"
    assert after.message_count == 2
    assert after.last_refreshed_at == before.last_refreshed_at


def test_an_empty_sweep_read_keeps_the_known_good_row(store):
    fake = account([1001])
    fake.threads[uid_for(1001)] = thread(uid_for(1001), [])

    remember(store, conversation_ref_for(1001), "2026-09-03T12:06:33")

    prepare_activity_index(fake.inbox(), store)

    after = store.for_conversation(conversation_ref_for(1001))

    assert after.last_message_at == "2026-09-03T12:06:33"
    assert after.message_count == 2


def test_a_timed_out_read_keeps_the_known_good_row(store):
    fake = account(
        [1001],
        thread_failures={uid_for(1001): httpx.ReadTimeout("scripted timeout")},
    )

    remember(store, conversation_ref_for(1001), "2026-09-03T12:06:33")

    row = build_inbox(fake.inbox(), store, limit=20).conversations[0]

    assert row["last_message_at"] == "2026-09-03T12:06:33"


# -- H. what may never enter the index --------------------------------------


def test_no_guest_text_or_provider_identifier_reaches_the_index(store):
    """The schema has no column that could hold either, asserted end to end.

    The booking ids are six digits so that the assertion cannot pass or fail by
    accident against a hex fingerprint that happens to contain them.
    """
    fake = account([700001, 700002])

    prepare_activity_index(fake.inbox(), store)
    build_inbox(fake.inbox(), store, limit=20)

    written = repr([row.__dict__ for row in store.all_activity()])

    assert GUEST_TEXT not in written
    assert "Fixture Guest" not in written
    assert "fixture.guest@example.invalid" not in written
    assert "+15550000000" not in written
    assert uid_for(700001) not in written
    assert "700001" not in written


def test_nothing_in_the_read_or_the_cycle_sends(store):
    fake = account([1001, 1002])

    prepare_activity_index(fake.inbox(), store)
    build_inbox(fake.inbox(), store, limit=20)

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)


# -- I. the route and the preparation cycle end to end ----------------------


@pytest.fixture
def index_api(api):
    """The reloaded app with a scripted Lodgify and a live refresh service.

    `conversation_refresh` is None in tests because the connector is
    unconfigured at import, so it has to be installed explicitly for the
    refresh route to do anything at all.
    """
    from app.conversation_refresh import ConversationRefreshService

    module = api.module

    fake = account([1001, 1002])

    module.lodgify_inbox = fake.inbox()
    module.conversation_refresh = ConversationRefreshService(
        inbox=module.lodgify_inbox,
        drafts=module.draft_store,
        agent=module.agent,
    )
    module.inbox_discovery.clear()

    api.fake = fake

    return api


def test_the_route_reports_a_warming_index_as_incomplete(index_api):
    """Nothing indexed yet: an empty page that says so rather than lying."""
    payload = index_api.client("ADMIN").get("/inbox").json()

    assert payload["conversations"] == []
    assert payload["count"] == 0
    assert payload["incomplete"] is True
    assert payload["activity_stale"] is False


def test_the_refresh_route_seeds_the_index(index_api):
    """`POST /inbox/refresh` is where a conversation enters the index."""
    admin = index_api.client("ADMIN")

    assert admin.post("/inbox/refresh").status_code == 200

    payload = admin.get("/inbox").json()

    assert payload["count"] == 2
    assert payload["incomplete"] is False


def test_the_route_reports_a_stale_ordering(index_api):
    module = index_api.module

    for booking_id in (1001, 1002):
        remember(
            module.activity_store,
            conversation_ref_for(booking_id),
            "2026-09-01T09:00:00",
        )
        backdate(
            module.activity_store,
            conversation_ref_for(booking_id),
            STALE_ACTIVITY_THRESHOLD_SECONDS + 60,
        )

    payload = index_api.client("ADMIN").get("/inbox").json()

    assert payload["count"] == 2
    assert payload["activity_stale"] is True


def test_the_route_never_seeds_however_often_it_is_refreshed(index_api):
    """Manual Refresh is a read. It must not be able to start a scan."""
    admin = index_api.client("ADMIN")

    for _ in range(3):
        assert admin.get("/inbox").status_code == 200

    assert index_api.module.activity_store.all_activity() == []
    assert len(index_api.fake.thread_reads) == 0


# -- J. discovery for the refresh path --------------------------------------


def test_discovery_includes_a_conversation_the_index_has_never_seen(store):
    """The anti-hiding property, and the reason discovery is not index-only.

    A brand-new booking, or one that progressive seeding has not reached yet,
    is absent from the index. If discovery enumerated the index alone, no reply
    would ever be prepared for it -- index staleness would silently decide who
    gets answered.
    """
    fake = account([1001, 1002])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")

    discovery = discover_conversations(fake.inbox(), store, limit=20)

    assert {ref for ref, _ in discovery.conversations} == {
        conversation_ref_for(1001),
        conversation_ref_for(1002),
    }


def test_an_empty_index_still_discovers_the_whole_current_ref_set(store):
    """Cold start is the worst case for an index-only discovery: it finds
    nothing at all, so the refresh prepares nothing at all."""
    live = list(range(1001, 1011))
    fake = account(live)

    discovery = discover_conversations(fake.inbox(), store, limit=20)

    assert {ref for ref, _ in discovery.conversations} == {
        conversation_ref_for(booking_id) for booking_id in live
    }


def test_discovery_costs_the_booking_scan_and_no_thread_reads(store):
    """The whole reason this may read the provider again.

    The live scan this replaced read a thread per conversation -- ~155 requests
    on most refresh cycles, because the preparation poll is longer than the
    discovery cache TTL. The booking scan does not grow with the account and
    reads no threads at all.
    """
    live = list(range(1001, 1031))
    fake = account(live)

    for index, booking_id in enumerate(live):
        remember(
            store, conversation_ref_for(booking_id), f"2026-09-{index + 1:02d}T09:00:00"
        )

    discover_conversations(fake.inbox(), store, limit=20)

    assert len(fake.booking_reads) == BOOKING_SCAN_PAGES
    assert len(fake.thread_reads) == 0
    assert len(fake.requests) == BOOKING_SCAN_PAGES


def test_an_indexed_ref_carries_its_fingerprint_and_an_unindexed_one_carries_none(
    store,
):
    """The empty fingerprint is the safe value, not a missing one.

    `refresh_inbox` uses it only as a pre-filter -- `draft_store.for_state`
    returns None for a fingerprint it has no draft for -- so an unindexed
    conversation is processed rather than skipped, and `process()` computes the
    authoritative fingerprint itself.
    """
    fake = account([1001, 1002])

    remember(store, conversation_ref_for(1001), "2026-09-01T09:00:00")

    found = dict(discover_conversations(fake.inbox(), store, limit=20).conversations)

    assert found[conversation_ref_for(1001)] == f"fp-{conversation_ref_for(1001)}"
    assert found[conversation_ref_for(1002)] == ""


def test_unindexed_refs_are_discovered_before_indexed_ones(store):
    """Ordering decides who gets a reply, because the refresh is bounded.

    A conversation AgentGuard has never evaluated is the one most likely to be
    waiting, and nothing else in the system will pick it up.
    """
    fake = account([1001, 1002, 1003])

    # Deliberately the newest activity there is, so index ordering alone would
    # put these first.
    remember(store, conversation_ref_for(1001), "2026-09-09T09:00:00")
    remember(store, conversation_ref_for(1003), "2026-09-08T09:00:00")

    discovery = discover_conversations(fake.inbox(), store, limit=20)

    assert [ref for ref, _ in discovery.conversations] == [
        conversation_ref_for(1002),
        conversation_ref_for(1001),
        conversation_ref_for(1003),
    ]


def test_indexed_refs_are_discovered_newest_first(store):
    """The index's own ordering intent, preserved."""
    fake = account([1001, 1002, 1003])

    remember(store, conversation_ref_for(1001), "2026-01-01T09:00:00")
    remember(store, conversation_ref_for(1002), "2026-09-09T09:00:00")
    remember(store, conversation_ref_for(1003), "2026-05-05T09:00:00")

    discovery = discover_conversations(fake.inbox(), store, limit=20)

    assert [ref for ref, _ in discovery.conversations] == [
        conversation_ref_for(1002),
        conversation_ref_for(1003),
        conversation_ref_for(1001),
    ]


def test_an_indexed_ref_with_no_known_activity_is_discovered_last(store):
    """Unknown is not newest. Nulls sort last here as they do in the index."""
    fake = account([1001, 1002])

    remember(store, conversation_ref_for(1001), None)
    remember(store, conversation_ref_for(1002), "2026-01-01T09:00:00")

    discovery = discover_conversations(fake.inbox(), store, limit=20)

    assert [ref for ref, _ in discovery.conversations] == [
        conversation_ref_for(1002),
        conversation_ref_for(1001),
    ]


def test_a_partial_booking_scan_makes_the_discovery_incomplete(store):
    """A page that was never read is not a page with nothing on it.

    One full booking page so the walk asks for a second, and the second fails.
    What was gathered is kept -- and reported as partial rather than as the
    whole account.
    """
    live = list(range(1001, 1001 + BOOKING_SCAN_SIZE))
    fake = account(live, booking_page_failures={2: 429})

    discovery = discover_conversations(fake.inbox(), store, limit=20)

    assert discovery.incomplete is True
    assert len(discovery.conversations) == 20


def test_a_complete_booking_scan_is_not_incomplete_by_itself(store):
    fake = account([1001, 1002])

    assert discover_conversations(fake.inbox(), store, limit=20).incomplete is False


def test_a_caller_supplied_incomplete_flag_is_preserved(store):
    """A warming index is still a partial picture, whatever the scan saw."""
    fake = account([1001])

    discovery = discover_conversations(fake.inbox(), store, limit=20, incomplete=True)

    assert discovery.incomplete is True


def test_the_discovery_limit_is_respected(store):
    live = list(range(1001, 1031))
    fake = account(live)

    for index, booking_id in enumerate(live):
        remember(
            store, conversation_ref_for(booking_id), f"2026-09-{index + 1:02d}T09:00:00"
        )

    discovery = discover_conversations(fake.inbox(), store, limit=5)

    assert len(discovery.conversations) == 5
    assert discovery.limit == 5


def test_discovery_holds_no_guest_text_or_provider_identifier(store):
    """The same seam guarantee the shared discovery has: refs and digests only."""
    fake = account([700001, 700002])

    remember(store, conversation_ref_for(700001), "2026-09-01T09:00:00")

    discovery = discover_conversations(fake.inbox(), store, limit=20)

    written = repr(discovery)

    assert GUEST_TEXT not in written
    assert "Fixture Guest" not in written
    assert "fixture.guest@example.invalid" not in written
    assert uid_for(700001) not in written
    assert "700001" not in written
