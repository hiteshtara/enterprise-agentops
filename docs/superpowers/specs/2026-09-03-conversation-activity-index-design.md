# Conversation activity index

**Status:** design, approved for spec review. Not implemented.
**Date:** 2026-09-03
**Milestone:** proactive drafting

## The problem

The Inbox enumerates conversations by paging Lodgify's booking list, because the
provider has no thread-list endpoint and a booking row carries no last-message
field. As of `5cb72d0` that scan covers `Current + Upcoming` — 152 threads,
~4s — deliberately not `All`, because reading a thread for each of the
account's 1062 bookings earns HTTP 429 and a throttled read fails closed to
`UNKNOWN`, which silently empties the top of the Inbox.

That leaves a gap. A guest whose stay has ended can still write, and a
**Historic** conversation is never enumerated, so it cannot appear however
recent its message is.

The webhook is the missing signal. A verified Lodgify webhook already names a
thread, already resolves to a `conversation_ref` through `find_by_thread`
(which searches every stay), and already drives `conversation_refresh.process`
to persist a draft. The draft is written and then has nowhere to appear,
because the listing never enumerates that conversation.

So the fix is not a bigger scan. It is to **persist what the webhook taught us**
and merge it into the listing.

## Non-goals

- **No brute-force polling of Historic stays.** The rate limit makes it
  unviable and it is the thing this design exists to avoid.
- **No guest contact domain.** Phone numbers and email are explicitly out of
  scope for this milestone and must not enter this table. A guest-contact store
  may be designed later, against a concrete workflow, as its own spec.
- **No message archive.** This index is metadata. Guest text stays live in
  Lodgify, transiently in model context, and — under the separate historical
  indexing rules — in sanitized historical reply storage. Nowhere else.
- **No change to sending.** Nothing here sends; that remains the DANGEROUS tool
  behind human approval.

## Storage

New table `conversation_activity`, one row per conversation, metadata only.

| column | type | note |
|---|---|---|
| `id` | int PK | |
| `conversation_ref` | str, unique, indexed | the safe reference; the natural key |
| `property_slug` | str, nullable | |
| `source` | str, nullable | channel the booking arrived through |
| `booking_status` | str, nullable | |
| `last_message_at` | str, nullable | the ordering signal |
| `last_message_sender` | str, nullable | |
| `message_count` | int | |
| `conversation_fingerprint` | str | |
| `status` | str | a `ConversationStatus` value |
| `first_seen_at` | str | |
| `last_refreshed_at` | str | |

**`property_name` is not stored.** It is derived from `property_slug` through
`LODGIFY_PROPERTIES`, so a configuration rename cannot leave stale display text
in the database.

**`needs_attention` is not stored.** It is derived from `status` everywhere, so
the two cannot disagree.

**Columns that must never exist here:** `thread_uid`, `booking_id`, any guest
name, email or phone, `last_message_excerpt`, any message body, any raw
provider payload. The absence is the safety property, and a test asserts the
column set explicitly rather than trusting review.

New module `app/conversation_activity.py`: a `ConversationActivity` dataclass
and a `ConversationActivityStore` taking an injected `Database`, matching
`DraftStore`. The model `ConversationActivityRecord` goes in `app/db_models.py`
alongside `ConversationDraftRecord`.

## Who writes the index

One index, two writers. There is deliberately no webhook-only storage.

**Webhook path** (fast). Verified signature → resolve `conversation_ref` →
refresh the thread through the supported API → **upsert activity metadata** →
proactive draft processing. The webhook payload stays non-authoritative and is
not persisted; it tells us *something changed*, and we re-read to find out what.

**Polling path** (recovery). The `Current + Upcoming` scan already computes a
`ConversationSummary` per conversation. It upserts the same rows. So the index
warms itself during normal operation, and a Historic row is simply one whose
last writer was a webhook.

Upsert is keyed on `conversation_ref`: newer activity overwrites metadata,
`first_seen_at` is preserved, `last_refreshed_at` moves. Re-delivery of the
same webhook is therefore idempotent.

## Reading: the merge

Composition does not belong in the connector — `LodgifyInbox` must not know the
database exists — and `main.py` is wiring and routes only. So a new module
`app/inbox_view.py` owns the merge and is what the route calls.

```
live Current+Upcoming summaries          (authoritative, fresh)
+ persisted activity rows                (metadata snapshot)
  -> dedupe by conversation_ref, live row wins where both exist
  -> sort by last_message_at descending
  -> apply the Inbox limit
  -> live-enrich only the persisted-only rows that survived onto the page
```

Ordering uses the **persisted** `last_message_at`, kept current by the webhook
and by the polling upsert. That is what keeps enrichment bounded: we never read
a thread in order to decide how to sort.

Sorting reuses the existing two-pass stable sort — `conversation_ref`
ascending, then `last_message_at` descending — so ties are deterministic.

## Reading: enrichment of persisted-only rows

A persisted-only row has no excerpt, because none is stored. For the rows that
survive onto the page we do one live read to supply it.

**The cost rule.** Resolving a `conversation_ref` means finding its booking,
and a Historic booking sits near the end of the archive — the one that exposed
this gap was at index 1044, page 11. Resolving each row independently would
re-page the whole archive per row, which is how the earlier 429 happened.

So: **one shared `all_bookings(stay_filters=("All",))` scan per Inbox request**,
performed only when persisted-only enrichment is actually needed, building an
in-memory `conversation_ref -> booking` map reused for every such row on that
request. The number of Historic rows must not multiply the archive paging cost.

This lands as a new connector method, `LodgifyInbox.summarise_refs(refs)`,
which does one scan plus one thread read per requested ref and returns safe
dicts. `ResolvedConversation` — which carries `booking_id` and `thread_uid` —
never leaves the connector, so the seam does not widen the identifier boundary.
Nothing may call `get_conversation(ref)` in a loop for this purpose.

**On success:** render the fresh excerpt, and update `conversation_activity` if
the read shows newer activity than the stored row.

**If enrichment discovers newer activity,** render the fresh result on this
response where practical. Global ordering becoming fully correct on the *next*
poll is acceptable; re-running the whole merge recursively is not. The lag is
at most one poll and applies only to a Historic conversation whose webhook we
already missed.

**On failure** — provider unavailable, rate-limited, or the ref resolves to
nothing:

- keep the persisted metadata row visible;
- render no excerpt; the API returns `last_message_excerpt: null` and the
  console shows a neutral "Preview unavailable";
- never drop the row, and never invent content.

Failing closed to *visible with less detail* is the point. A conversation that
needs attention must not disappear because a preview could not be fetched.

## Retention

Historic rows are **not** aged out because the stay is old — a conversation can
matter after checkout, which is the whole reason this index exists. For V1:
retain rows, let newer activity overwrite metadata for the same
`conversation_ref`, no automatic deletion. If the table grows materially, add a
retention or archive policy driven by business need rather than booking age.

## Migration

A new, reviewed Alembic migration creating `conversation_activity`. The model
is imported in `alembic/env.py` so autogenerate does not propose dropping it.
Existing migrations are not altered. The migration creates structure only and
seeds nothing.

## Console

The only frontend change is rendering a neutral "Preview unavailable" when
`last_message_excerpt` is null on a row that has activity. Because frontend
code changes, `npm run test`, `typecheck`, `lint` and `build` apply.

## Testing

Invented data only; no live provider calls. Backend:

1. a Historic conversation identified by a verified webhook appears in the Inbox
2. live `Current+Upcoming` and persisted rows merge into one list
3. dedupe by `conversation_ref`
4. the live row wins where a conversation exists in both
5. ordering by `last_message_at` descending across merged sources
6. the limit is applied after merge and ordering
7. a persisted-only row that survives the page is enriched with a live excerpt
8. a failed enrichment read keeps the metadata row visible with no invented preview
9. a repeated webhook updates the same row idempotently
10. the table has no guest-text column and no excerpt is ever persisted
11. no provider identifiers (`booking_id`, `thread_uid`) are persisted
12. a Historic row survives ordinary `Current+Upcoming` polling
13. enrichment pages the booking archive **once** per request regardless of how
    many persisted-only rows are on the page

Test 13 is the regression guard for the 429 that made the naive full scan
unshippable; it asserts a request count, not a timing.

## Risks

- **Enrichment still costs one archive scan** (~11 booking-list calls) on any
  request with a persisted-only row on the page. Bounded per request, not per
  row, but not free. If it proves noticeable, the next step is caching the
  resolution map briefly in-process — deliberately not in V1.
- **A Historic conversation whose webhook never arrived stays invisible.** The
  index only knows what a webhook or a live scan told it. This design closes
  the gap for *delivered* webhooks; it does not make discovery exhaustive.
- **Ordering can lag by one poll** for a persisted-only row whose activity moved
  without a webhook. Accepted above, deliberately.

## Open questions

None blocking. The guest-contact domain is deferred to its own spec with a
concrete workflow, and is not a dependency of this work.
