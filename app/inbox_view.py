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

from typing import Any

from app.connectors.lodgify.config import LODGIFY_PROPERTIES
from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import DEFAULT_LIMIT, LodgifyInbox
from app.conversation_activity import ConversationActivityStore

# Display name is derived, never stored, so a rename in the property table
# cannot leave stale text sitting in a persisted row. Unknown slugs map to
# nothing rather than raising -- a stale or removed property must not break
# the Inbox.
PROPERTY_NAME_BY_SLUG = {prop.slug: prop.display_name for prop in LODGIFY_PROPERTIES}


def build_inbox(
    inbox: LodgifyInbox,
    activity_store: ConversationActivityStore,
    property_slug: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """One page of the Inbox, newest conversation activity first."""
    live = inbox.list_conversations(property_slug=property_slug, limit=limit)

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

    return page


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
