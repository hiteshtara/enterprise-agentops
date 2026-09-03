"""Composing the Inbox from what we remember and what we can still see.

The Inbox is a view of *conversation activity*, so it orders by the time of
each conversation's last message. The provider does not offer that time
cheaply: a booking row carries no last-message field, its `updated_at` does not
move when a message arrives, and there is no thread-list endpoint
(`docs/LODGIFY_API.md` sections 8 and 15, both verified live). Reading it means
reading the thread.

That is what made the old Inbox cost ~155 provider requests: it read a thread
for every current-and-upcoming conversation purely to decide the sort order,
and was caught live returning HTTP 429 on the first booking page, which fails
the whole request.

So ordering comes from the persisted activity index instead:

    index query, ordered and limited        0 provider calls
    Current+Upcoming booking scan           ~3 calls  -- resolves refs, and
                                                         reveals unseen ones
    thread read for each row on the page     N calls  -- N = the page size
    one shared archive scan                 +11 calls -- only if a Historic
                                                         row made the page

The booking scan is the floor and is not optional: `thread_uid` is deliberately
never persisted, so a ref can only be turned back into a thread by matching it
against live bookings. That is a privacy choice, and this is its cost.

`GET /inbox` **never seeds**. A conversation the booking scan reveals but the
index has not seen is not rendered; the page is flagged incomplete instead. So
no read -- Manual Refresh included -- can trigger an unbounded scan, and the
Inbox says "there is more than this" rather than presenting a short list as the
whole truth. Seeding and re-reading are the preparation cycle's job alone; see
`prepare_activity_index`.

The trade this makes, stated plainly: ordering is as fresh as the index. A
message that arrives with no webhook, on a conversation not currently on the
page, can be up to one sweep cycle late. That is accepted, and it is why the
page reports `activity_stale` rather than presenting the index as current.

This lives above the connector because it touches the database, and the
connector must not know the database exists. It lives outside `main.py` because
that file is wiring and routes only.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.connectors.lodgify.config import LODGIFY_PROPERTIES, LODGIFY_SLUGS
from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import (
    DEFAULT_LIMIT,
    LodgifyInbox,
    ResolvedConversation,
    validate_limit,
)
from app.connectors.lodgify.messaging_client import INBOX_STAY_FILTERS
from app.conversation_activity import ConversationActivity, ConversationActivityStore
from app.timing import MonotonicNs, default_monotonic_ns

# Display name is derived, never stored, so a rename in the property table
# cannot leave stale text sitting in a persisted row. Unknown slugs map to
# nothing rather than raising -- a stale or removed property must not break
# the Inbox.
PROPERTY_NAME_BY_SLUG = {prop.slug: prop.display_name for prop in LODGIFY_PROPERTIES}

# How many never-seen conversations one preparation cycle reads.
#
# Deliberately the same budget as a sweep. The obvious cold start -- read every
# thread once -- is the same ~157-request burst that was caught live returning
# 429, which would make the Inbox fail hardest exactly when the database is
# new. A brand-new database instead reaches full coverage over several cycles
# at steady-state cost, and reports itself incomplete until it gets there.
SEED_BATCH = 25

# How many already-indexed conversations one preparation cycle re-reads.
#
# This is the recovery path for a webhook that never arrived, and the only
# guarantee this design offers about an unnoticed message: every conversation
# is re-read within one full cycle. That cycle grows linearly with the number
# of conversations in the account -- worth revisiting when the account roughly
# doubles.
SWEEP_SIZE = 25

# When the ordering behind a page stops being presentable as current.
#
# One full sweep cycle at this account size plus margin. Past it the console
# says the ordering may be behind; it never hides a row and never reads as an
# error, because it is not one.
STALE_ACTIVITY_THRESHOLD_SECONDS = 900

# How long one discovery scan may stand in for the next.
#
# The console polls the Inbox and asks for a refresh on separate cadences, and
# a refresh arriving seconds after a poll used to repeat the identical scan. 60
# seconds covers that overlap with room for a slow scan, and is short enough
# that a refresh cycle never works from a picture older than half its own
# interval.
#
# Nothing correctness-bearing rides on it. A stale fingerprint can only cost one
# redundant `process()` call, which re-reads the conversation and recomputes the
# fingerprint authoritatively before spending anything.
DISCOVERY_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class Discovery:
    """What one Inbox discovery found, reduced to the shareable minimum.

    `(conversation_ref, fingerprint)` pairs and nothing else. Deliberately not
    the rendered rows: those carry `last_message_excerpt`, which is guest text,
    and guest text does not belong in a process-wide structure with a lifetime
    nobody asked for. A ref is an opaque token and a fingerprint is a digest --
    neither says anything about a guest.

    `limit` is carried because a discovery is only an answer to the question it
    was asked. Twenty rows cannot stand in for a request for fifty.
    """

    conversations: tuple[tuple[str, str], ...]
    limit: int
    incomplete: bool = False


class DiscoveryCache:
    """The most recent discovery, for as long as it is worth reusing.

    Deliberately holds one entry. This exists so that one user action costs one
    scan, not so that scans are avoided: `GET /inbox` never reads from it and
    always discovers live, because the Inbox is the thing that has to be true.

    The clock is monotonic and injectable, like every other duration in the
    codebase, so expiry is tested by advancing a counter rather than sleeping.
    """

    def __init__(
        self,
        ttl_seconds: float = DISCOVERY_TTL_SECONDS,
        monotonic_ns: MonotonicNs | None = None,
    ) -> None:
        self._ttl_ns = int(ttl_seconds * 1_000_000_000)
        self._monotonic_ns = monotonic_ns or default_monotonic_ns
        self._entry: tuple[int, Discovery] | None = None

    def put(self, discovery: Discovery) -> None:
        self._entry = (self._monotonic_ns(), discovery)

    def recent(self, limit: int) -> Discovery | None:
        """The last discovery, if it is fresh and wide enough to answer `limit`."""
        if self._entry is None:
            return None

        stored_at, discovery = self._entry

        if self._monotonic_ns() - stored_at >= self._ttl_ns:
            self._entry = None

            return None

        if discovery.limit < limit:
            # It was asked a narrower question than this caller is asking.
            return None

        return discovery

    def clear(self) -> None:
        self._entry = None


@dataclass(frozen=True)
class InboxResult:
    """One page of the Inbox, plus what the page knows about itself.

    `incomplete` says the list may be short: either discovery lost a booking
    page, or a conversation exists that the index has not read yet.
    `activity_stale` says the ordering may be behind. Neither hides a row and
    neither is an error.

    `discovery` is what is safe to share with the refresh path, and it is None
    for a property-filtered page -- see `build_inbox` for why. A caller that
    finds one there may publish it without checking anything else.
    """

    conversations: list[dict[str, Any]] = field(default_factory=list)
    incomplete: bool = False
    discovery: Discovery | None = None
    activity_stale: bool = False


@dataclass(frozen=True)
class IndexCycle:
    """What one preparation cycle did, in counts only.

    `unseeded_remaining` is how the caller knows warm-up is still in progress
    without a mode flag anywhere: coverage is complete when it reaches zero.
    """

    seeded: int = 0
    swept: int = 0
    unseeded_remaining: int = 0
    incomplete: bool = False


def discovery_from(
    rows: list[dict[str, Any]], limit: int, incomplete: bool
) -> Discovery:
    """Reduce Inbox rows to the pairs that are safe to keep."""
    return Discovery(
        conversations=tuple(
            (row["conversation_ref"], row.get("fingerprint") or "") for row in rows
        ),
        limit=limit,
        incomplete=incomplete,
    )


def discover_conversations(
    inbox: LodgifyInbox,
    activity_store: ConversationActivityStore,
    limit: int = DEFAULT_LIMIT,
    incomplete: bool = False,
) -> Discovery:
    """One discovery, for a caller that has no shared one to work from.

    The fallback behind `POST /inbox/refresh`: if no Inbox poll has run
    recently there is nothing to reuse, and a refresh that discovered nothing
    would do nothing.

    **Which conversations exist is asked of the bookings, never of the index.**
    The index is a cache of what has been *read*, and it is legitimately behind:
    empty at cold start, partial while progressive seeding works through its
    batches, and missing a booking made a minute ago. Enumerating it would mean
    a conversation it has not reached is simply absent from the ref set, so no
    reply is ever prepared for it -- index staleness silently deciding who gets
    answered. The Current+Upcoming booking scan is the authoritative list of
    conversations that can currently need a reply, and it costs ~3 provider
    calls and no thread reads at all. That is the whole reason this reads the
    provider again rather than the live scan it replaced, which read a thread
    per conversation -- 155 requests, on most refresh cycles, which is what was
    caught live returning 429 and 502.

    **An unindexed ref carries an empty fingerprint, and that is safe.** The
    fingerprint is only ever a pre-filter: `refresh_inbox` asks
    `draft_store.for_state(ref, fingerprint)`, which returns None for a
    fingerprint it has no draft for, so the conversation is *processed* rather
    than skipped. `process()` re-reads the conversation and computes the real
    fingerprint itself before spending anything. A missing or stale fingerprint
    here can therefore cost one redundant read, and can never cost a missed
    reply -- the failure direction the whole function is arranged around.

    A partial booking scan is reported as partial rather than being presented
    as the whole account, for the same reason `build_inbox` reports it: a page
    that was never read is not a page with nothing on it.
    """
    scan = inbox.scan_bookings(stay_filters=INBOX_STAY_FILTERS)

    indexed = {row.conversation_ref: row for row in activity_store.all_activity()}

    ordered = _discovery_order(
        list(dict.fromkeys(booking.conversation_ref for booking in scan.bookings)),
        indexed,
    )

    return Discovery(
        conversations=tuple(
            (ref, _indexed_fingerprint(indexed.get(ref))) for ref in ordered[:limit]
        ),
        limit=limit,
        incomplete=incomplete or scan.incomplete,
    )


def _indexed_fingerprint(activity: ConversationActivity | None) -> str:
    """The stored fingerprint for a ref, or the empty string for an unread one.

    Empty means "no pre-filter available", which is the safe value: it can only
    cause `refresh_inbox` to process a conversation it might have skipped.
    """
    return (activity.conversation_fingerprint or "") if activity is not None else ""


def _discovery_order(
    refs: list[str],
    indexed: dict[str, ConversationActivity],
) -> list[str]:
    """Never-evaluated conversations first, then the index's own ordering.

    The refresh is limit-bounded and its per-poll budget is smaller still, so
    this order decides who actually gets a reply prepared, not merely who is
    listed first. A conversation AgentGuard has never evaluated is the one most
    likely to be waiting on an answer, and it is the one nothing else in the
    system will pick up; an indexed conversation has already been through
    `process()` at least once. So unindexed refs outrank indexed ones, the same
    priority `prepare_activity_index` applies to seeding over sweeping.

    Within the indexed refs the intent of `ordered_activity` is preserved --
    newest `last_message_at` first, unknown timestamps last, ties broken on the
    reference ascending -- so a refresh working from the index and a refresh
    working from a shared Inbox discovery agree on what matters most.
    """
    unindexed = sorted(ref for ref in refs if ref not in indexed)

    timed = sorted(
        ref
        for ref in refs
        if ref in indexed and indexed[ref].last_message_at is not None
    )

    # Stable, so the ascending-reference sort above survives as the tie-break.
    timed.sort(
        key=lambda ref: indexed[ref].last_message_at,
        reverse=True,
    )

    undated = sorted(
        ref for ref in refs if ref in indexed and indexed[ref].last_message_at is None
    )

    return unindexed + timed + undated


def build_inbox(
    inbox: LodgifyInbox,
    activity_store: ConversationActivityStore,
    property_slug: str | None = None,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> InboxResult:
    """One page of the Inbox, newest conversation activity first.

    Ordering costs zero provider calls: it is an indexed, bounded query against
    `conversation_activity`. The provider is asked two things and no more --
    which bookings are current or upcoming (so a ref can be turned back into a
    thread, and so a conversation the index has never seen is revealed), and
    what the threads on this page say now.

    A ref the booking scan reveals but the index has not seen is deliberately
    *not* rendered and *not* written. Seeding here would let a Manual Refresh
    trigger an unbounded scan, which is the failure this whole change removes.
    The page reports `incomplete` instead.
    """
    count = validate_limit(limit)

    if property_slug is not None and property_slug not in LODGIFY_SLUGS:
        # Raised, not coerced, so the route answers 400 rather than quietly
        # showing the whole account when one property was asked for.
        raise ValueError(
            f"Unknown property: {property_slug!r}. Valid properties: "
            f"{', '.join(LODGIFY_SLUGS)}."
        )

    scan = inbox.scan_bookings(stay_filters=INBOX_STAY_FILTERS)

    bookings_by_ref = {
        booking.conversation_ref: booking
        for booking in scan.bookings
        if property_slug is None or booking.property_slug == property_slug
    }

    # Known to exist, never read. Not renderable, but very much reportable.
    unseeded = set(bookings_by_ref) - activity_store.known_refs(bookings_by_ref)

    page = [
        _row_from(activity)
        for activity in activity_store.ordered_activity(
            count,
            property_slug=property_slug,
        )
    ]

    # Measured *before* enrichment, because enrichment refreshes exactly these
    # rows -- afterwards every page would report itself current and the flag
    # would never fire. What it answers is "how old was the ordering that chose
    # this page", which is the question an operator is actually asking.
    activity_stale = _is_stale(
        activity_store.oldest_refreshed_at(row["conversation_ref"] for row in page),
        now=now,
    )

    _enrich(inbox, activity_store, page, bookings_by_ref)

    incomplete = scan.incomplete or bool(unseeded)

    return InboxResult(
        conversations=page,
        incomplete=incomplete,
        activity_stale=activity_stale,
        # A filtered page answers a question about one property, so it is not
        # an answer to the question a refresh asks -- which is about the whole
        # account. Sharing it would quietly narrow what gets a prepared reply.
        discovery=(
            discovery_from(page, limit=count, incomplete=incomplete)
            if property_slug is None
            else None
        ),
    )


def prepare_activity_index(
    inbox: LodgifyInbox,
    activity_store: ConversationActivityStore,
    seed_batch: int = SEED_BATCH,
    sweep_size: int = SWEEP_SIZE,
) -> IndexCycle:
    """One bounded cycle of index maintenance: seed what is new, sweep the rest.

    The only place a conversation is added to the index by polling, and the
    only place an already-indexed conversation is re-read without being on a
    page. Both halves are bounded, so a cycle costs about the same whether the
    database is empty or warm, and never a thread read per conversation in the
    account.

    **Unseeded work always outranks sweep work.** Re-reading an already-indexed
    row while a conversation has never been read at all is spending the budget
    on the less valuable half. There is no mode flag for warm-up: the condition
    *is* "no unseeded refs remain".

    A webhook naturally takes precedence over both -- a row it has already
    written is not unseeded, so a batch will not read it again.

    A thread that will not load is left alone rather than recorded as empty, so
    a provider failure mid-warm-up costs progress it never made and never
    progress it did.
    """
    scan = inbox.scan_bookings(stay_filters=INBOX_STAY_FILTERS)

    bookings_by_ref = {booking.conversation_ref: booking for booking in scan.bookings}

    unseeded = sorted(set(bookings_by_ref) - activity_store.known_refs(bookings_by_ref))

    if unseeded:
        seeded = _seed(inbox, activity_store, bookings_by_ref, unseeded[:seed_batch])

        return IndexCycle(
            seeded=seeded,
            swept=0,
            unseeded_remaining=len(unseeded) - seeded,
            incomplete=True,
        )

    swept = _sweep(inbox, activity_store, bookings_by_ref, sweep_size)

    return IndexCycle(
        seeded=0,
        swept=swept,
        unseeded_remaining=0,
        incomplete=scan.incomplete,
    )


def _seed(
    inbox: LodgifyInbox,
    store: ConversationActivityStore,
    bookings_by_ref: dict[str, ResolvedConversation],
    refs: list[str],
) -> int:
    """Read one bounded batch of never-seen conversations into the index.

    Nothing is preserved here because nothing is known yet: a row that does not
    exist has nothing to lose, so a genuinely empty thread is recorded as empty
    and counts as seeded. An *unreadable* thread is absent from `found` and is
    simply left for the next cycle.
    """
    found = _observe(inbox, bookings_by_ref, set(refs))

    for summary in found.values():
        _remember(store, summary)

    return len(found)


def _sweep(
    inbox: LodgifyInbox,
    store: ConversationActivityStore,
    bookings_by_ref: dict[str, ResolvedConversation],
    sweep_size: int,
) -> int:
    """Re-read the least-recently-refreshed slice of the index.

    The recovery path for a message that arrived with no webhook. The same
    preserve rules as the read path apply: a read that did not happen, or that
    came back strictly less informative than what is already stored, writes
    nothing.
    """
    rows = store.least_recently_refreshed(sweep_size)

    if not rows:
        return 0

    found = _observe(inbox, bookings_by_ref, {row.conversation_ref for row in rows})

    refreshed = 0

    for activity in rows:
        fresh = found.get(activity.conversation_ref)

        if fresh is None:
            continue

        if not _supersedes(_row_from(activity), fresh):
            continue

        _remember(store, fresh)

        refreshed += 1

    return refreshed


def _observe(
    inbox: LodgifyInbox,
    bookings_by_ref: dict[str, ResolvedConversation],
    refs: set[str],
) -> dict[str, dict[str, Any]]:
    """Live summaries for `refs`, by the cheapest route each one allows.

    A ref the Current+Upcoming scan already resolved needs only its thread
    read. Every other ref is Historic -- reachable only through the archive --
    and they go together in **one** shared scan, because resolving them one at
    a time re-pages the archive per ref and reaches the provider's rate limit.

    Absence carries one meaning and one only: *we did not observe this
    conversation*. A thread that would not load, a ref that matched no booking,
    and an archive scan that failed outright are all absences, never empties.
    """
    direct = [bookings_by_ref[ref] for ref in sorted(refs) if ref in bookings_by_ref]

    remote = {ref for ref in refs if ref not in bookings_by_ref}

    found = {
        summary.conversation_ref: summary.to_dict()
        for summary in inbox.summarise_readable(direct)
    }

    if not remote:
        return found

    try:
        found.update(inbox.summarise_refs(remote))

    except LodgifyUnavailable:
        # Every remote row keeps its stored metadata and shows no preview.
        pass

    return found


def _row_from(activity: ConversationActivity) -> dict[str, Any]:
    """One Inbox row built from the index alone, before anything is read."""
    return dict(
        activity.to_row(),
        property_name=PROPERTY_NAME_BY_SLUG.get(activity.property_slug),
        last_message_excerpt=None,
        preview_unavailable=True,
    )


def _is_stale(oldest_refreshed_at: str | None, now: datetime | None = None) -> bool:
    """Whether the ordering behind a page is old enough to say so.

    None is not stale: no row was found, and an empty page is not a behind one.
    An unparsable timestamp is not stale either -- guessing would put a notice
    on the page for a reason nobody could explain.
    """
    if oldest_refreshed_at is None:
        return False

    try:
        refreshed = datetime.fromisoformat(oldest_refreshed_at)

    except ValueError:
        return False

    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=UTC)

    moment = now or datetime.now(UTC)

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    return (moment - refreshed).total_seconds() > STALE_ACTIVITY_THRESHOLD_SECONDS


def _remember(store: ConversationActivityStore, row: dict[str, Any]) -> None:
    """Upsert one live summary into the index. Never stores guest text."""
    store.upsert(
        conversation_ref=row["conversation_ref"],
        conversation_fingerprint=row.get("fingerprint") or "",
        status=row["status"],
        last_message_at=row.get("last_message_at"),
        last_message_sender=row.get("last_message_sender"),
        message_count=row.get("message_count") or 0,
        property_slug=row.get("property_slug"),
        source=row.get("source"),
        booking_status=row.get("booking_status"),
    )


def _has_known_good_activity(row: dict[str, Any]) -> bool:
    """Whether a persisted row already records real conversation activity.

    Either field alone is enough: a timestamp with no count, or a count with
    no timestamp, both still mean *something happened here before*. A missing
    `message_count` is read as 0, never as unknown -- the field is always
    populated on a persisted row (see `ConversationActivity.message_count`),
    so `None` can only reach here from a hand-built test row.
    """
    return row.get("last_message_at") is not None or (row.get("message_count") or 0) > 0


def _is_empty_summary(summary: dict[str, Any]) -> bool:
    """Whether a *successful* live read came back with no messages.

    Deliberately the same shape check as `_has_known_good_activity`, inverted,
    because it answers the same question about a different dict -- whether a
    thread read observed anything at all.
    """
    return (
        summary.get("last_message_at") is None
        and (summary.get("message_count") or 0) == 0
    )


def _supersedes(known: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """Whether a fresh observation may replace what is already known.

    A *present* summary can still be less informative than what is stored: 42
    threads in the real account return an empty `messages` array while still
    advertising a `last_message_date`, so an empty read is not proof the
    conversation is actually empty -- it is as likely a provider quirk as an
    outage is. Overwriting known-good activity with that empty read would sink
    a Historic conversation permanently, because the Current+Upcoming scan can
    never rediscover it and repair the damage.

    A row with no known-good activity has nothing to lose, so its empty read is
    trusted as-is. That is not a special case; it is what already happened
    before this guard existed.
    """
    return not (_has_known_good_activity(known) and _is_empty_summary(fresh))


def _enrich(
    inbox: LodgifyInbox,
    store: ConversationActivityStore,
    page: list[dict[str, Any]],
    bookings_by_ref: dict[str, ResolvedConversation],
) -> None:
    """Re-read the threads on this page, and only the threads on this page.

    Every row arrives here rendered from the index, so this is what makes a
    page live: it supplies the excerpt, the current status, and the current
    message count for the rows an operator is about to look at. A row we cannot
    read stays visible with its stored metadata and no preview; it is never
    dropped and nothing is invented for it.

    Absence from `_observe` means *we did not observe this conversation*, and
    the only safe response is to keep what is already known. Enriching such a
    row would write an unread conversation's nulls over the one record that a
    Historic conversation moved, and the live scan can never rediscover it.

    A *partial* archive scan needs no separate handling: a ref whose booking
    sat on a page that never answered is simply not in `found`, which already
    means "not observed". The guards hold whether the scan failed entirely,
    failed half way through, or succeeded.
    """
    pending = {row["conversation_ref"] for row in page if row["preview_unavailable"]}

    if not pending:
        return

    found = _observe(inbox, bookings_by_ref, pending)

    for index, row in enumerate(page):
        fresh = found.get(row["conversation_ref"])

        if fresh is None:
            # Not observed. Keep the persisted metadata, the persisted
            # ordering position, `preview_unavailable=True` and no excerpt,
            # and write nothing back to the index.
            continue

        if not _supersedes(row, fresh):
            # Observed, but a strictly worse observation than what is already
            # known. Same treatment as absence.
            page[index] = dict(
                row,
                last_message_excerpt=None,
                preview_unavailable=True,
            )
            continue

        page[index] = dict(fresh, preview_unavailable=False)

        _remember(store, fresh)
