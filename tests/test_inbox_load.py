"""Provider load: one discovery scan per action, cached resolution, partial scans.

Three measured production failures sit behind this file. Against the live
account (1062 bookings, 11 archive pages, 152 Current+Upcoming threads):

  * `GET /inbox` cost 155 provider requests and `POST /inbox/refresh` ran the
    very same 155-request discovery scan again, seconds later, before doing any
    work -- 350-445 requests inside one console poll cycle, which is how a
    provider starts answering 429;
  * `resolve()` walked 7-11 `stayFilter=All` booking pages *per call*, and one
    refreshed conversation resolves more than once;
  * one failed booking page raised out of `list_conversations` and turned the
    whole Inbox into a 502, discarding 151 perfectly good thread reads.

Everything here runs through `httpx.MockTransport`. No socket is opened, no
credential is real, no guest data is real, and nothing sends.
"""

import pytest

from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import (
    BOOKING_SCAN_SIZE,
    RESOLUTION_CACHE_TTL_SECONDS,
    ResolutionCache,
)
from app.connectors.lodgify.messaging_client import INBOX_STAY_FILTERS, STAY_FILTER_ALL
from app.connectors.lodgify.refs import conversation_ref_for
from app.conversation_activity import ConversationActivityStore
from app.inbox_view import DiscoveryCache, build_inbox
from tests.lodgify_fakes import FAKE_KEY, FakeLodgify, booking, message, thread

# -- fixtures, all invented -------------------------------------------------

GUEST_TEXT = "Invented fixture question from a guest."

OWNER_TEXT = "Invented fixture answer from the owner."


class Clock:
    """A monotonic nanosecond source a test drives by hand.

    The caches are the only thing in this file that depends on the passage of
    time, and a test that slept would be both slow and flaky. Advancing a
    counter is exact.
    """

    def __init__(self) -> None:
        self.nanoseconds = 0

    def __call__(self) -> int:
        return self.nanoseconds

    def advance(self, seconds: float) -> None:
        self.nanoseconds += int(seconds * 1_000_000_000)


def answered(booking_id: int, uid: str, at: str) -> tuple[dict, dict]:
    """One booking whose thread ends with our own reply.

    Owner-last on purpose: `analyse_conversation` settles it deterministically,
    so a refresh over these conversations costs provider reads and no model
    call at all. That is what makes a request-count assertion meaningful.
    """
    return (
        booking(booking_id, uid),
        thread(uid, [message(f"m-{uid}", "Owner", OWNER_TEXT, at)]),
    )


def archive(entries: list[tuple[int, str, str]], **kwargs) -> FakeLodgify:
    bookings = []
    threads = {}

    for booking_id, uid, at in entries:
        row, payload = answered(booking_id, uid, at)
        bookings.append(row)
        threads[uid] = payload

    return FakeLodgify(bookings=bookings, threads=threads, **kwargs)


def full_first_page(**kwargs) -> FakeLodgify:
    """Exactly one full booking page, so the walk always asks for a second."""
    return archive(
        [
            (2000 + index, f"quiet-{index}", "2026-01-01T09:00:00")
            for index in range(BOOKING_SCAN_SIZE)
        ],
        **kwargs,
    )


def stay_filters(fake: FakeLodgify) -> list[str | None]:
    return [request.url.params.get("stayFilter") for request in fake.booking_reads]


def discovery_reads(fake: FakeLodgify) -> list[str | None]:
    """Only the booking pages an Inbox *discovery* scan issues.

    Discovery enumerates `INBOX_STAY_FILTERS`; every other archive walk asks
    for `All`. Counting by filter separates "we scanned the Inbox again" from
    "we resolved one conversation", which a bare request count cannot.
    """
    return [value for value in stay_filters(fake) if value in INBOX_STAY_FILTERS]


def all_reads(fake: FakeLodgify) -> list[str | None]:
    return [value for value in stay_filters(fake) if value == STAY_FILTER_ALL]


@pytest.fixture
def activity(database):
    return ConversationActivityStore(database=database)


# -- A. one user action, one discovery scan ---------------------------------


@pytest.fixture
def refresh_api(api):
    """The reloaded app with a scripted Lodgify and a live refresh service.

    `conversation_refresh` is None in tests because the connector is
    unconfigured at import, so it has to be installed explicitly for the
    refresh route to do anything at all.
    """
    from app.conversation_refresh import ConversationRefreshService

    module = api.module

    fake = archive(
        [
            (1001, "thread-a", "2026-09-01T10:00:00"),
            (1002, "thread-b", "2026-09-02T10:00:00"),
            (1003, "thread-c", "2026-09-03T10:00:00"),
        ]
    )

    module.lodgify_inbox = fake.inbox()
    module.conversation_refresh = ConversationRefreshService(
        inbox=module.lodgify_inbox,
        drafts=module.draft_store,
        agent=module.agent,
    )
    module.inbox_discovery.clear()

    api.fake = fake

    return api


def test_refresh_does_not_repeat_the_inbox_discovery_scan(refresh_api):
    """The 502 that started this: two full scans for one user action."""
    fake = refresh_api.fake
    admin = refresh_api.client("ADMIN")

    assert admin.get("/inbox").status_code == 200

    after_poll = len(discovery_reads(fake))
    threads_after_poll = len(fake.thread_reads)

    assert after_poll == len(INBOX_STAY_FILTERS)
    assert threads_after_poll == 3

    assert admin.post("/inbox/refresh").status_code == 200

    # Not one more discovery page. The refresh consumed what the poll found.
    assert len(discovery_reads(fake)) == after_poll

    # And not one more discovery thread read either: the only new thread reads
    # are the authoritative per-conversation reads the refresh itself needs.
    assert len(fake.thread_reads) - threads_after_poll <= 3


def test_refresh_processes_the_refs_the_poll_discovered(refresh_api):
    admin = refresh_api.client("ADMIN")

    page = admin.get("/inbox")

    discovered = {row["conversation_ref"] for row in page.json()["conversations"]}

    result = admin.post("/inbox/refresh")

    assert result.status_code == 200, result.text

    payload = result.json()

    # The response shape is unchanged: counts only, never guest text.
    assert set(payload) == {"processed", "drafted", "skipped", "no_reply", "failed"}
    assert payload["processed"] == 3

    module = refresh_api.module

    prepared = set(module.draft_store.latest_by_conversation())

    assert prepared == discovered


def test_refresh_discovers_once_itself_when_nothing_recent_is_shared(refresh_api):
    """No poll has run. The refresh may scan -- once."""
    fake = refresh_api.fake

    assert refresh_api.client("ADMIN").post("/inbox/refresh").status_code == 200

    assert len(discovery_reads(fake)) == len(INBOX_STAY_FILTERS)


def test_two_overlapping_refreshes_do_not_multiply_the_scan(refresh_api):
    fake = refresh_api.fake
    admin = refresh_api.client("ADMIN")

    assert admin.post("/inbox/refresh").status_code == 200

    after_first = len(discovery_reads(fake))

    assert admin.post("/inbox/refresh").status_code == 200

    assert len(discovery_reads(fake)) == after_first


def test_the_shared_discovery_holds_no_guest_text(refresh_api):
    """Only `(conversation_ref, fingerprint)` may cross this seam.

    An excerpt is guest text. Sharing rendered inbox rows would put it in a
    process-wide structure with a lifetime nobody asked for.
    """
    admin = refresh_api.client("ADMIN")

    assert admin.get("/inbox").status_code == 200

    shared = refresh_api.module.inbox_discovery.recent(20)

    assert shared is not None
    assert OWNER_TEXT not in repr(shared)
    assert "fixture.guest@example.invalid" not in repr(shared)
    assert "Fixture Guest" not in repr(shared)


def test_a_refresh_sends_nothing(refresh_api):
    admin = refresh_api.client("ADMIN")

    admin.get("/inbox")
    admin.post("/inbox/refresh")

    assert refresh_api.fake.posts == []
    assert all(request.method == "GET" for request in refresh_api.fake.requests)


def test_the_request_budget_for_one_poll_cycle(refresh_api):
    """The number this whole file exists to hold down.

    Three conversations, one booking page. Written as exact counts so that a
    change which quietly reintroduces a second discovery scan fails here with
    a number rather than as an intermittent 502 in production.
    """
    fake = refresh_api.fake
    admin = refresh_api.client("ADMIN")

    assert admin.get("/inbox").status_code == 200

    #  2 booking pages -- one per Inbox stay filter
    #  3 thread reads  -- one per candidate conversation
    assert len(fake.booking_reads) == 2
    assert len(fake.thread_reads) == 3

    poll = len(fake.requests)

    assert poll == 5

    assert admin.post("/inbox/refresh").status_code == 200

    #  0 booking pages for discovery -- reused from the poll
    #  3 booking pages, one per conversation resolved for its authoritative read
    #  3 thread reads
    refresh = len(fake.requests) - poll

    assert refresh == 6

    # Before this change the refresh repeated discovery in full: 2 + 3 for the
    # scan on top of the 6 it actually needed.
    assert refresh < poll + 6


def test_resolving_the_same_conversation_twice_walks_the_archive_once():
    """One refreshed conversation resolves more than once -- for the thread
    read, for the turnover question, for the extension window. Live, each walk
    was 7-11 booking pages."""
    fake = resolving_fake()
    inbox = fake.inbox(resolutions=ResolutionCache(monotonic_ns=Clock()))

    ref = conversation_ref_for(9001)

    inbox.get_conversation(ref)

    after_read = len(all_reads(fake))

    inbox.turnover_for(ref)

    # `turnover_for` still reads departures live; it just does not re-resolve.
    assert len(all_reads(fake)) == after_read + 2


# -- B. the resolution cache ------------------------------------------------


def resolving_fake() -> FakeLodgify:
    """An archive deep enough that resolution costs more than one page."""
    entries = [
        (2000 + index, f"quiet-{index}", "2026-01-01T09:00:00")
        for index in range(BOOKING_SCAN_SIZE)
    ]

    entries.append((9001, "thread-late", "2026-09-03T10:00:00"))

    return archive(entries)


def test_a_second_resolution_costs_no_booking_pages():
    fake = resolving_fake()
    inbox = fake.inbox(resolutions=ResolutionCache(monotonic_ns=Clock()))

    ref = conversation_ref_for(9001)

    inbox.resolve(ref)

    first = len(all_reads(fake))

    assert first == 2

    inbox.resolve(ref)

    assert len(all_reads(fake)) == first


def test_an_expired_resolution_is_read_again():
    clock = Clock()

    fake = resolving_fake()
    inbox = fake.inbox(resolutions=ResolutionCache(monotonic_ns=clock))

    ref = conversation_ref_for(9001)

    inbox.resolve(ref)

    first = len(all_reads(fake))

    clock.advance(RESOLUTION_CACHE_TTL_SECONDS + 1)

    inbox.resolve(ref)

    assert len(all_reads(fake)) == first * 2


def test_a_cache_miss_always_falls_back_to_a_live_read():
    """Losing the cache must cost correctness nothing at all."""
    fake = resolving_fake()
    cache = ResolutionCache(monotonic_ns=Clock())
    inbox = fake.inbox(resolutions=cache)

    ref = conversation_ref_for(9001)

    before = inbox.resolve(ref)

    cache.clear()

    assert inbox.resolve(ref) == before


def test_the_resolution_cache_holds_no_guest_text_or_credential():
    fake = resolving_fake()
    cache = ResolutionCache(monotonic_ns=Clock())
    inbox = fake.inbox(resolutions=cache)

    inbox.resolve(conversation_ref_for(9001))

    held = repr(cache.entries())

    assert cache.entries()

    for forbidden in (
        "Fixture Guest",
        "fixture.guest@example.invalid",
        "+15550000000",
        "internal note that must not travel",
        "confirmationCode",
        FAKE_KEY,
    ):
        assert forbidden not in held


def test_the_resolution_cache_does_not_grow_without_bound():
    """A server runs for weeks; the archive is 1062 bookings."""
    from app.connectors.lodgify.inbox import MAX_CACHED_RESOLUTIONS

    clock = Clock()

    entries = [
        (5000 + index, f"held-{index}", "2026-01-01T09:00:00")
        for index in range(MAX_CACHED_RESOLUTIONS + 1)
    ]

    fake = archive(entries)
    cache = ResolutionCache(monotonic_ns=clock)
    inbox = fake.inbox(resolutions=cache)

    for booking_id, _uid, _at in entries:
        inbox.resolve(conversation_ref_for(booking_id))
        clock.advance(1)

    # Everything older than the TTL has been swept; nothing live was dropped.
    assert len(cache.entries()) <= MAX_CACHED_RESOLUTIONS


def test_an_unknown_ref_is_never_cached_into_existence():
    fake = resolving_fake()
    cache = ResolutionCache(monotonic_ns=Clock())
    inbox = fake.inbox(resolutions=cache)

    with pytest.raises(ValueError):
        inbox.resolve(conversation_ref_for(4242))

    assert cache.entries() == ()


# -- C. a partial archive scan --------------------------------------------


def test_a_first_page_failure_with_nothing_gathered_still_raises():
    fake = archive(
        [(1001, "thread-a", "2026-09-01T10:00:00")],
        booking_page_failures={1: 503},
    )

    with pytest.raises(LodgifyUnavailable):
        fake.inbox().list_conversations()


def test_a_later_page_failure_returns_what_was_gathered():
    fake = full_first_page(booking_page_failures={2: 503})

    scan = fake.inbox().scan_conversations(limit=100)

    assert len(scan.conversations) == 100
    assert scan.incomplete is True


def test_a_later_page_failure_is_never_read_as_the_end_of_the_archive():
    """The silent-truncation trap: a failed page is not an empty page."""
    complete = full_first_page().inbox().scan_conversations(limit=100)

    partial = full_first_page(booking_page_failures={2: 503})
    partial_scan = partial.inbox().scan_conversations(limit=100)

    assert complete.incomplete is False
    assert partial_scan.incomplete is True

    # Same rows either way -- the difference is only that one of them admits
    # it may not have seen everything.
    assert [row["conversation_ref"] for row in complete.conversations] == [
        row["conversation_ref"] for row in partial_scan.conversations
    ]


def test_a_partial_scan_lists_no_conversation_twice():
    fake = full_first_page(booking_page_failures={2: 503})

    listed = [
        row["conversation_ref"]
        for row in fake.inbox().scan_conversations(limit=100).conversations
    ]

    assert len(listed) == len(set(listed))


def test_complete_pagination_is_unchanged():
    """The ordinary path must not have grown a behaviour."""
    fake = full_first_page()

    scan = fake.inbox().scan_conversations(limit=100)

    assert scan.incomplete is False
    assert len(scan.conversations) == 100
    assert fake.inbox().list_conversations(limit=100) == scan.conversations


# -- C. incompleteness reaches the API, and the index survives --------------


HISTORIC_REF = conversation_ref_for(770001)


def remember_historic(store: ConversationActivityStore) -> None:
    """A Historic conversation the live scan can never rediscover.

    This is the row with the most to lose: the activity index is the only
    record that it moved, so a partial scan overwriting it with nulls would
    sink it to the bottom of the Inbox permanently.
    """
    store.upsert(
        conversation_ref=HISTORIC_REF,
        conversation_fingerprint="fp-historic",
        status="needs_attention",
        last_message_at="2026-09-04T18:00:00",
        last_message_sender="Renter",
        message_count=4,
        property_slug="renovated-3rd-floor-retreat-3-beds-roslindale-village",
        source="BookingCom",
        booking_status="Booked",
    )


def test_a_partial_scan_marks_the_inbox_incomplete(activity):
    fake = full_first_page(booking_page_failures={2: 503})

    result = build_inbox(fake.inbox(), activity, limit=20)

    assert result.incomplete is True
    assert len(result.conversations) == 20


def test_a_complete_scan_is_not_marked_incomplete(activity):
    fake = full_first_page()

    assert build_inbox(fake.inbox(), activity, limit=20).incomplete is False


def test_the_api_reports_an_incomplete_inbox(refresh_api):
    module = refresh_api.module

    fake = full_first_page(booking_page_failures={2: 503})
    module.lodgify_inbox = fake.inbox()

    response = refresh_api.client("ADMIN").get("/inbox")

    assert response.status_code == 200, response.text
    assert response.json()["incomplete"] is True


def test_the_api_still_502s_when_nothing_could_be_gathered(refresh_api):
    module = refresh_api.module

    fake = archive(
        [(1001, "thread-a", "2026-09-01T10:00:00")],
        booking_page_failures={1: 503},
    )
    module.lodgify_inbox = fake.inbox()

    response = refresh_api.client("ADMIN").get("/inbox")

    assert response.status_code == 502, response.text


def test_a_known_activity_row_survives_a_failed_booking_page(activity):
    remember_historic(activity)

    fake = full_first_page(booking_page_failures={2: 503})

    result = build_inbox(fake.inbox(), activity, limit=20)

    assert HISTORIC_REF in {row["conversation_ref"] for row in result.conversations}

    stored = activity.for_conversation(HISTORIC_REF)

    assert stored is not None
    assert stored.last_message_at == "2026-09-04T18:00:00"
    assert stored.message_count == 4


def test_a_partial_scan_never_overwrites_known_good_metadata(activity):
    remember_historic(activity)

    before = activity.for_conversation(HISTORIC_REF)

    fake = full_first_page(booking_page_failures={2: 503})

    row = next(
        row
        for row in build_inbox(fake.inbox(), activity, limit=20).conversations
        if row["conversation_ref"] == HISTORIC_REF
    )

    # Rendered from the preserved snapshot, not from an unknown/null read.
    assert row["last_message_at"] == "2026-09-04T18:00:00"
    assert row["message_count"] == 4
    assert row["preview_unavailable"] is True
    assert row["last_message_excerpt"] is None

    after = activity.for_conversation(HISTORIC_REF)

    assert after is not None
    assert before is not None
    assert after.last_refreshed_at == before.last_refreshed_at


def test_a_partial_scan_sends_nothing(activity):
    fake = full_first_page(booking_page_failures={2: 503})

    build_inbox(fake.inbox(), activity, limit=20)

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)


# -- the shared discovery structure itself ---------------------------------


def test_the_discovery_cache_expires():
    clock = Clock()
    cache = DiscoveryCache(monotonic_ns=clock)

    from app.inbox_view import Discovery

    cache.put(Discovery(conversations=(("PH-AAAAAAAA", "fp"),), limit=20))

    assert cache.recent(20) is not None

    clock.advance(3600)

    assert cache.recent(20) is None


def test_a_narrower_discovery_is_not_reused_for_a_wider_request():
    cache = DiscoveryCache(monotonic_ns=Clock())

    from app.inbox_view import Discovery

    cache.put(Discovery(conversations=(("PH-AAAAAAAA", "fp"),), limit=5))

    assert cache.recent(5) is not None
    assert cache.recent(20) is None
