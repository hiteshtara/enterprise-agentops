"""Composing the Inbox from what we can see and what we remember.

The connector enumerates conversations by paging the booking list, and that
scan covers current and upcoming stays only -- the archive is too large to read
a thread for each without being rate-limited. So the live scan alone cannot
show a Historic conversation, however recent its message.

The activity index remembers those. This module puts the two together:

    live Current+Upcoming summaries      (authoritative, fresh)
    + persisted activity rows            (metadata snapshot)
      -> dedupe by conversation_ref, live wins
      -> order by last_message_at
      -> apply the limit
      -> enrich only the persisted-only rows that survived

Ordering uses the *persisted* timestamp, which is what keeps enrichment
bounded: no thread is ever read in order to decide the sort order.

This lives above the connector because it touches the database, and the
connector must not know the database exists. It lives outside `main.py`
because that file is wiring and routes only.
"""

from dataclasses import dataclass, field
from typing import Any

from app.connectors.lodgify.config import LODGIFY_PROPERTIES
from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import DEFAULT_LIMIT, LodgifyInbox
from app.conversation_activity import ConversationActivityStore
from app.timing import MonotonicNs, default_monotonic_ns

# Display name is derived, never stored, so a rename in the property table
# cannot leave stale text sitting in a persisted row. Unknown slugs map to
# nothing rather than raising -- a stale or removed property must not break
# the Inbox.
PROPERTY_NAME_BY_SLUG = {prop.slug: prop.display_name for prop in LODGIFY_PROPERTIES}

# How long one discovery scan may stand in for the next.
#
# The console polls the Inbox every 30s and asks for a refresh every 120s, so
# every fourth poll is followed within seconds by a refresh that used to repeat
# the identical 155-request scan. 60 seconds covers that overlap with room for a
# slow scan, and is short enough that a refresh cycle never works from a picture
# older than half its own interval.
#
# Nothing correctness-bearing rides on it. A stale fingerprint can only cost one
# redundant `process()` call, which re-reads the conversation and recomputes the
# fingerprint authoritatively before spending anything.
DISCOVERY_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class Discovery:
    """What one Inbox discovery scan found, reduced to the shareable minimum.

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
    """One page of the Inbox, plus what the scan behind it knows about itself.

    `discovery` is what is safe to share with the refresh path, and it is None
    for a property-filtered page -- see `build_inbox` for why. A caller that
    finds one there may publish it without checking anything else.
    """

    conversations: list[dict[str, Any]] = field(default_factory=list)
    incomplete: bool = False
    discovery: Discovery | None = None


def discovery_from(
    rows: list[dict[str, Any]], limit: int, incomplete: bool
) -> Discovery:
    """Reduce live summaries to the pairs that are safe to keep."""
    return Discovery(
        conversations=tuple(
            (row["conversation_ref"], row.get("fingerprint") or "") for row in rows
        ),
        limit=limit,
        incomplete=incomplete,
    )


def discover_conversations(
    inbox: LodgifyInbox,
    limit: int = DEFAULT_LIMIT,
) -> Discovery:
    """One discovery scan, for a caller that has no shared one to work from.

    The fallback behind `POST /inbox/refresh`: if no Inbox poll has run
    recently there is nothing to reuse, and a refresh that discovered nothing
    would do nothing. It scans once, and what it finds is publishable in turn.
    """
    scan = inbox.scan_conversations(limit=limit)

    return discovery_from(scan.conversations, limit=limit, incomplete=scan.incomplete)


def build_inbox(
    inbox: LodgifyInbox,
    activity_store: ConversationActivityStore,
    property_slug: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> InboxResult:
    """One page of the Inbox, newest conversation activity first."""
    scan = inbox.scan_conversations(property_slug=property_slug, limit=limit)

    live = scan.conversations

    # The live read is authoritative, so it refreshes the index on the way past.
    for row in live:
        _remember(activity_store, row)

    by_ref = {
        row["conversation_ref"]: dict(row, preview_unavailable=False) for row in live
    }

    for activity in activity_store.all_activity():
        if activity.conversation_ref in by_ref:
            # Live wins: a snapshot never overrides something just read.
            continue

        if property_slug is not None and activity.property_slug != property_slug:
            continue

        by_ref[activity.conversation_ref] = dict(
            activity.to_row(),
            property_name=PROPERTY_NAME_BY_SLUG.get(activity.property_slug),
            last_message_excerpt=None,
            preview_unavailable=True,
        )

    merged = list(by_ref.values())

    # Two stable passes: the reference breaks ties ascending while the
    # timestamp sorts descending, so the order is deterministic.
    merged.sort(key=lambda row: row["conversation_ref"])
    merged.sort(key=lambda row: row["last_message_at"] or "", reverse=True)

    page = merged[:limit]

    _enrich(inbox, activity_store, page)

    return InboxResult(
        conversations=page,
        incomplete=scan.incomplete,
        # A filtered scan enumerated one property, so it is not an answer to
        # the question a refresh asks -- which is about the whole account.
        # Sharing it would quietly narrow what gets a prepared reply.
        discovery=(
            discovery_from(live, limit=limit, incomplete=scan.incomplete)
            if property_slug is None
            else None
        ),
    )


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


def _enrich(
    inbox: LodgifyInbox,
    store: ConversationActivityStore,
    page: list[dict[str, Any]],
) -> None:
    """Supply a live excerpt for persisted-only rows that made the page.

    One archive scan for all of them together -- see
    `LodgifyInbox.summarise_refs`. A row we cannot read stays visible with no
    preview; it is never dropped and nothing is invented for it.

    Absence from `summarise_refs` means *we did not observe this conversation*
    -- the archive no longer explains the ref, or the thread would not load --
    and the only safe response is to keep what is already known. Enriching such
    a row would write an unread conversation's nulls over the one record that a
    Historic conversation moved, and the live scan can never rediscover it.

    A *present* summary can still be less informative than what is already
    known: 42 threads in the real account return an empty `messages` array
    while still advertising a `last_message_date`, so an empty read is not
    proof the conversation is actually empty -- it is as likely a provider
    quirk as an outage is. Overwriting known-good persisted activity with that
    empty read would sink a Historic conversation exactly as permanently as
    the failure case above does, so the same preserve-and-do-not-upsert rule
    applies whenever the fresh read would make the row strictly less
    informative than what is already on it. A row with no known-good activity
    has nothing to lose, so its empty read is trusted as-is -- this is not a
    special case, it is what already happened before this guard existed.

    A *partial* archive scan needs no new handling here: a ref whose booking
    sat on a page that never answered is simply not in `found`, which already
    means "not observed". The guards below hold whether the scan failed
    entirely, failed half way through, or succeeded.
    """
    pending = {row["conversation_ref"] for row in page if row["preview_unavailable"]}

    if not pending:
        return

    try:
        found = inbox.summarise_refs(pending)

    except LodgifyUnavailable:
        # Every pending row keeps its stored metadata and shows no preview.
        return

    for index, row in enumerate(page):
        fresh = found.get(row["conversation_ref"])

        if fresh is None:
            # Not observed. Keep the persisted metadata, the persisted
            # ordering position, `preview_unavailable=True` and no excerpt,
            # and write nothing back to the index.
            continue

        if _has_known_good_activity(row) and _is_empty_summary(fresh):
            # Observed, but a strictly worse observation than what is already
            # known. Same treatment as absence: keep the persisted metadata
            # and ordering position, show no preview, and write nothing back.
            page[index] = dict(
                row,
                last_message_excerpt=None,
                preview_unavailable=True,
            )
            continue

        page[index] = dict(fresh, preview_unavailable=False)

        _remember(store, fresh)
