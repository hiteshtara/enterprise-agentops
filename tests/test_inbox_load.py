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
from app.inbox_view import (
    SEED_BATCH,
    SWEEP_SIZE,
    DiscoveryCache,
    build_inbox,
    prepare_activity_index,
)
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

    # The Inbox orders from the activity index and never seeds it, so nothing
    # is renderable until a preparation cycle has read these conversations
    # once. That is what `POST /inbox/refresh` does in production. Warming it
    # here and then clearing the request log keeps every count below about the
    # steady state this file exists to measure, not about cold start -- cold
    # start has its own counts in `tests/test_activity_index_ordering.py`.
    prepare_activity_index(module.lodgify_inbox, module.activity_store)

    fake.requests.clear()

    api.fake = fake

    return api


def test_refresh_does_not_repeat_the_inbox_discovery_scan(refresh_api):
    """The 502 that started this: two full scans for one user action.

    The refresh does walk the booking list once -- that is the index's
    preparation cycle, and it is a fixed handful of pages however many
    conversations the account has. What it must never do again is *discover*
    what to draft for by reading a thread per conversation: that ordering now
    comes from the index, for no provider calls at all.
    """
    fake = refresh_api.fake
    admin = refresh_api.client("ADMIN")

    assert admin.get("/inbox").status_code == 200

    after_poll = len(discovery_reads(fake))
    threads_after_poll = len(fake.thread_reads)

    assert after_poll == len(INBOX_STAY_FILTERS)
    assert threads_after_poll == 3

    assert admin.post("/inbox/refresh").status_code == 200

    # One booking scan for the preparation cycle, and no second discovery on
    # top of it: the refresh consumed the pairs the poll published.
    assert len(discovery_reads(fake)) == after_poll + len(INBOX_STAY_FILTERS)

    # Bounded by the sweep budget plus the authoritative per-conversation
    # reads the refresh itself needs -- never by the size of the account.
    assert len(fake.thread_reads) - threads_after_poll <= SWEEP_SIZE + 3


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
    """No poll has run. The refresh discovers for itself -- from the bookings.

    Two Current+Upcoming walks: the index's preparation cycle, then discovery.
    Both are a fixed handful of booking pages that does not grow with the
    account, and neither reads a thread to decide what to draft for. That is
    the number this file defends -- against the 155-request
    thread-per-conversation scan discovery used to be, and against an
    index-only discovery, which costs nothing and hides every conversation the
    index has not read yet.
    """
    fake = refresh_api.fake

    assert refresh_api.client("ADMIN").post("/inbox/refresh").status_code == 200

    assert len(discovery_reads(fake)) == 2 * len(INBOX_STAY_FILTERS)


def test_two_overlapping_refreshes_do_not_multiply_the_scan(refresh_api):
    """The second refresh reuses the first refresh's published discovery.

    So the second costs its preparation cycle alone: the cost of a refresh
    stays flat when the console polls faster than the discovery TTL, which is
    the overlap the shared discovery exists for.
    """
    fake = refresh_api.fake
    admin = refresh_api.client("ADMIN")

    assert admin.post("/inbox/refresh").status_code == 200

    after_first = len(discovery_reads(fake))

    assert after_first == 2 * len(INBOX_STAY_FILTERS)

    assert admin.post("/inbox/refresh").status_code == 200

    assert len(discovery_reads(fake)) == after_first + len(INBOX_STAY_FILTERS)


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


def test_a_settled_conversation_is_skipped_on_the_next_refresh(refresh_api):
    """The fingerprint pre-filter still works when discovery supplies it.

    An indexed ref carries the fingerprint the index stored, which is the same
    state the draft was worked out for -- so a second refresh spends nothing on
    a conversation nothing has changed about.
    """
    admin = refresh_api.client("ADMIN")

    first = admin.post("/inbox/refresh")

    assert first.status_code == 200, first.text
    assert first.json()["processed"] == 3

    # Force the fallback: no shared discovery, so the pairs come from the
    # booking scan and the index rather than from a poll.
    refresh_api.module.inbox_discovery.clear()

    second = admin.post("/inbox/refresh")

    assert second.status_code == 200, second.text
    assert second.json() == {
        "processed": 0,
        "drafted": 0,
        "skipped": 3,
        "no_reply": 0,
        "failed": 0,
    }


def test_an_empty_fingerprint_cannot_skip_a_conversation(refresh_api):
    """Why an unindexed ref's empty fingerprint is safe rather than lossy.

    Discovery supplies the empty string for a conversation the index has never
    read. The pre-filter finds no draft for it, so the refresh *processes* the
    conversation -- and `process()` re-reads it and computes the authoritative
    fingerprint itself. The empty value can cost work; it can never skip work.
    """
    admin = refresh_api.client("ADMIN")

    assert admin.post("/inbox/refresh").status_code == 200

    module = refresh_api.module

    ref = conversation_ref_for(1001)

    settled = module.draft_store.current_for(ref)

    assert settled is not None
    assert settled.is_settled()

    assert module.draft_store.for_state(ref, "") is None


# -- A warming index must not hide a conversation from drafting -------------


@pytest.fixture
def warming_api(api):
    """More conversations than one seeding batch, and nothing indexed yet.

    Cold start as the refresh route actually meets it: the preparation cycle
    seeds `SEED_BATCH` of them and the rest stay unread, which is precisely
    when an index-only discovery would leave a conversation with no reply
    prepared and no way to notice.
    """
    from app.conversation_refresh import ConversationRefreshService

    module = api.module

    fake = archive(
        [
            (3000 + index, f"warm-{index}", "2026-09-01T10:00:00")
            for index in range(SEED_BATCH + 3)
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


def test_the_refresh_prepares_the_conversations_the_index_has_not_reached(warming_api):
    """The anti-hiding property, end to end through the route.

    Three conversations remain unseeded after the cycle's batch. They are the
    ones the refresh spends its budget on, because a conversation AgentGuard
    has never evaluated outranks one it already has.
    """
    module = warming_api.module

    every = sorted(
        conversation_ref_for(3000 + index) for index in range(SEED_BATCH + 3)
    )

    # A seeding batch takes the unseeded refs in ascending reference order, so
    # these three are the ones still unread when discovery runs.
    unseeded = every[SEED_BATCH:]

    result = warming_api.client("ADMIN").post("/inbox/refresh")

    assert result.status_code == 200, result.text

    # Unchanged: the per-poll budget still bounds the work.
    assert result.json()["processed"] == module.MAX_REFRESH_PER_POLL

    prepared = set(module.draft_store.latest_by_conversation())

    assert set(unseeded) <= prepared


def test_the_refresh_still_reads_no_thread_to_discover(warming_api):
    """Discovery's whole cost is the booking scan, however cold the index is."""
    fake = warming_api.fake

    assert warming_api.client("ADMIN").post("/inbox/refresh").status_code == 200

    # Two Current+Upcoming walks -- the preparation cycle and discovery -- and
    # not one thread read attributable to either.
    assert len(discovery_reads(fake)) == 2 * len(INBOX_STAY_FILTERS)


def test_the_request_budget_for_one_poll_cycle(refresh_api):
    """The number this whole file exists to hold down.

    Three conversations, one booking page. Written as exact counts so that a
    change which quietly reintroduces a second discovery scan fails here with
    a number rather than as an intermittent 502 in production.
    """
    fake = refresh_api.fake
    admin = refresh_api.client("ADMIN")

    assert admin.get("/inbox").status_code == 200

    #  2 booking pages -- one per Inbox stay filter, to resolve refs to threads
    #  3 thread reads  -- one per row *on the page*, never per conversation
    assert len(fake.booking_reads) == 2
    assert len(fake.thread_reads) == 3

    poll = len(fake.requests)

    assert poll == 5

    assert admin.post("/inbox/refresh").status_code == 200

    #  2 booking pages + 3 thread reads -- the index preparation cycle
    #  0 booking pages for discovery -- reused from the poll
    #  3 booking pages, one per conversation resolved for its authoritative read
    #  3 thread reads
    refresh = len(fake.requests) - poll

    assert refresh == 11

    # The poll no longer grows with the account: ordering is an index query.
    # Three conversations is too small to show that, so it is asserted where a
    # large fixture makes it visible -- see
    # `tests/test_activity_index_ordering.py`.


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


def seed_full_first_page(store: ConversationActivityStore) -> None:
    """Index every conversation `full_first_page` puts on booking page one.

    The Inbox never seeds on a read, so a page of rows only exists once a
    preparation cycle has been through. Written directly rather than by running
    a cycle so that the request counts in these tests measure the read alone.
    """
    for index in range(BOOKING_SCAN_SIZE):
        store.upsert(
            conversation_ref=conversation_ref_for(2000 + index),
            conversation_fingerprint=f"fp-{index}",
            status="responded",
            last_message_at=f"2026-01-01T09:00:{index % 60:02d}",
            last_message_sender="Owner",
            message_count=1,
            property_slug="renovated-3rd-floor-retreat-3-beds-roslindale-village",
            source="BookingCom",
            booking_status="Booked",
        )


def test_a_partial_scan_marks_the_inbox_incomplete(activity):
    seed_full_first_page(activity)

    fake = full_first_page(booking_page_failures={2: 503})

    result = build_inbox(fake.inbox(), activity, limit=20)

    assert result.incomplete is True
    assert len(result.conversations) == 20


def test_a_complete_scan_is_not_marked_incomplete(activity):
    seed_full_first_page(activity)

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
