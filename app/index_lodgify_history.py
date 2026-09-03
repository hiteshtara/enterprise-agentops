"""Build or refresh the historical reply index.

Explicit and manual, by design. Nothing indexes on import, nothing re-downloads
the archive at server startup, and no background job exists to forget about.
Run it when the archive has moved on:

    uv run --env-file .env python -m app.index_lodgify_history

Read-only with respect to Lodgify: it issues the same two documented GETs the
Inbox already uses, and never sends, updates or marks anything.

Output is counts only. A progress line naming a property is fine; a progress
line quoting a guest is not, so nothing here prints message content.
"""

import sys

from app.connectors.lodgify.config import is_configured, resolve_api_key
from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import LodgifyInbox
from app.connectors.lodgify.messaging_client import LodgifyMessagingClient
from app.database import Database
from app.historical_replies import (
    HistoricalReplyStore,
    IndexReport,
    extract_exchanges,
)

# One page of bookings is what the Inbox reads; the archive wants everything the
# account has.
ARCHIVE_PAGE_SIZE = 100

MAX_PAGES = 20


def build_index(
    inbox: LodgifyInbox,
    store: HistoricalReplyStore,
    max_pages: int = MAX_PAGES,
    progress=None,
) -> IndexReport:
    """Walk the archive and upsert every extractable exchange.

    Each booking is read once. A thread that cannot be read is counted and
    skipped rather than aborting the build -- a single bad thread should not
    cost the whole index.
    """
    report = IndexReport()

    seen_threads: set[str] = set()
    exchanges = []

    for page in range(1, max_pages + 1):
        try:
            bookings = inbox.booking_page(page=page, size=ARCHIVE_PAGE_SIZE)

        except LodgifyUnavailable:
            break

        if not bookings:
            break

        for booking in bookings:
            report.bookings_scanned += 1

            if booking.thread_uid in seen_threads:
                report.skipped += 1
                continue

            seen_threads.add(booking.thread_uid)

            try:
                thread = inbox.thread_for_indexing(booking.thread_uid)

            except LodgifyUnavailable:
                report.thread_errors += 1
                continue

            report.threads_read += 1

            found = extract_exchanges(
                [message.to_dict() for message in thread.messages],
                property_slug=booking.property_slug,
                source=booking.source,
                # The guest's own name and email, removed from message bodies
                # by value. Neither is persisted.
                identities=thread.identities,
            )

            exchanges.extend(found)

            if progress is not None and report.threads_read % 25 == 0:
                progress(f"  threads read: {report.threads_read}")

        if len(bookings) < ARCHIVE_PAGE_SIZE:
            break

    # Deduplicate across threads before writing: the same exchange can appear
    # on two bookings that share a thread.
    unique = {exchange.example_ref: exchange for exchange in exchanges}

    report.exchanges_extracted = len(unique)
    report.skipped += len(exchanges) - len(unique)

    report.created, report.updated = store.upsert(list(unique.values()))

    return report


def main() -> int:
    if not is_configured():
        print("LODGIFY_API_KEY is not set; nothing to index.", file=sys.stderr)

        return 1

    database = Database()

    inbox = LodgifyInbox(LodgifyMessagingClient(api_key_provider=resolve_api_key))
    store = HistoricalReplyStore(database=database)

    print("Indexing Lodgify history (read-only)…")

    report = build_index(inbox, store, progress=print)

    print()
    print(f"Bookings scanned:     {report.bookings_scanned}")
    print(f"Threads read:         {report.threads_read}")
    print(f"Thread errors:        {report.thread_errors}")
    print(f"Exchanges extracted:  {report.exchanges_extracted}")
    print(f"New examples:         {report.created}")
    print(f"Updated:              {report.updated}")
    print(f"Skipped:              {report.skipped}")
    print(f"Index size:           {store.count()}")

    database.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
