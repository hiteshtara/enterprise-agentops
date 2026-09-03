# Activity-index ordering for the Inbox

**Status:** design, approved for spec review. Not implemented.
**Date:** 2026-09-03
**Follows:** `2026-09-03-conversation-activity-index-design.md`

## The problem

`GET /inbox` costs about 155 provider requests: three booking-list pages, then a
thread read for every one of the ~152 current-and-upcoming conversations. It
reads all of them for one reason — to learn each conversation's last-message
time, which is what the Inbox orders by.

At a thirty-second poll that was ~5 requests/second sustained, and it was caught
live returning **HTTP 429 on the first booking page**, which fails the whole
request and surfaced as a 502. Commit `d40700c` raised the poll to three minutes
as an operational ceiling and said plainly it was not the fix. This is the fix.

## The constraint everything follows from

**The provider offers no cheap way to learn which threads changed.** This is
settled, not assumed:

- a booking row carries no last-message field — verified across all 1062
  bookings, the only messaging field is `thread_uid`;
- sending a message leaves the booking's `updated_at` **unmoved**
  (`docs/LODGIFY_API.md` §15, verified live) — so no `updatedSince`-style filter
  can find message activity;
- there is no thread-list endpoint (`docs/LODGIFY_API.md` §8).

So freshness can come only from a push, a sweep, or accepting bounded staleness.
This design uses all three, deliberately, and makes the staleness visible.

## The trade, stated plainly

Today's Inbox is always exactly correct because it re-reads everything. After
this change, ordering is as fresh as the index. A message that arrives with no
webhook, on a conversation not currently on the page, can be up to one sweep
cycle late — roughly twelve minutes at this account size.

That is accepted. The console must therefore **show** when activity may be out
of date rather than presenting the index as perfectly current. Silence is the
failure mode this design is most concerned with.

## Constants

| Name | Value | Why |
|---|---|---|
| `SWEEP_SIZE` | 25 | Bounded per-poll cost; full coverage of ~154 conversations in ~6 polls. |
| preparation poll | 120s | Unchanged; already exists as `PREPARE_MS`. |
| `STALE_ACTIVITY_THRESHOLD` | 15 minutes | One sweep cycle plus margin. |
| `SEED_BATCH` | 25 | Cold-start seeding, deliberately the same budget as a sweep. |

V1 defaults, chosen against a measured budget. Do not tune them without a live
measurement that justifies the change.

## Ordering

`conversation_activity.last_message_at` is already indexed
(`ix_conversation_activity_last_message_at`), so ordering becomes a bounded
`ORDER BY last_message_at DESC LIMIT n` query. This replaces today's
`all_activity()` full-table load into Python, retiring a Minor finding from the
previous review.

Ties break on `conversation_ref` ascending, matching the existing two-pass sort,
so ordering stays deterministic.

## `GET /inbox`

```
1. index query, ordered and limited                    0 provider calls
2. Current+Upcoming booking scan (3 pages)             3 calls
     - resolves refs to threads (thread_uid is never persisted, so this
       scan is not optional; it is the floor)
     - reveals any ref the index has never seen
3. read threads for the rows on the page               N calls (N = limit)
4. Historic row on the page -> one shared All scan    +11 calls, only if present
```

`GET /inbox` **never seeds**. Seeding is the preparation cycle's job alone, so
no read -- including Manual Refresh -- can ever trigger an unbounded scan. A ref
the booking scan reveals but the index has not seen is not rendered yet; the
page is flagged incomplete instead, which is how the Inbox says "there is more
than this" rather than quietly presenting a short list as the whole truth.

Steps 3 and 4 already exist as `_enrich` / `summarise_refs` and keep their
one-shared-scan property: the archive is paged once per request regardless of
how many rows need it.

**Target: ~3 + 20 = 23 requests**, against 155 today.

## Seeding a new conversation

The Current+Upcoming booking scan is the authoritative set of refs, and it is
cheap: three calls, no thread reads. Comparing it against the index is what
identifies *unseeded* conversations -- known to exist, activity not yet read.

Seeding happens only in the preparation cycle, in bounded batches, never on a
read. A conversation therefore enters the index by one of three routes: a
verified webhook naming it, a seeding batch, or the sweep.

## The rotating sweep

The preparation poll no longer re-reads everything. It re-reads the
**least-recently-refreshed `SWEEP_SIZE` rows**, ordered by `last_refreshed_at`
ascending, and updates their activity metadata.

This is the recovery path for a missed webhook, and it is the only guarantee
this design offers about unnoticed messages: **every conversation is re-read
within one full cycle** — ~6 polls, ~12 minutes at this account size. The cycle
lengthens as the account grows; that relationship should be stated in the code
so it is not forgotten.

**Target: ~3 + 25 = 28 requests** per recovery poll, plus whatever model calls
drafting makes.

## Staleness

The API reports the oldest `last_refreshed_at` among the returned rows. When
that exceeds `STALE_ACTIVITY_THRESHOLD`, the page is flagged and the console
shows a neutral line — reusing the `incomplete` warning pattern already built
for partial discovery rather than inventing a second vocabulary.

Stale is not an error and must not hide rows. It says: this ordering may be
behind, and the sweep has not caught up.

## Cold start: bounded and progressive

An empty index cannot order anything, and the obvious fix -- read every thread
once -- is the same ~157-request burst that was **caught live returning 429**.
Paying it at startup means the Inbox can fail hardest exactly when the database
is new. So cold start is progressive, and the Inbox is honest about it.

Each preparation cycle:

1. compare the Current+Upcoming refs against the index;
2. if any are unseeded, read up to `SEED_BATCH` of them and index them;
3. only when none remain unseeded does the cycle do rotating-sweep work.

**Unseeded work always outranks sweep work.** Re-reading an already-indexed row
while a conversation has never been read at all is spending the budget on the
less valuable half. A brand-new database therefore reaches full coverage in
about six cycles -- roughly twelve minutes -- at the same per-cycle cost as
steady state, and never in one burst.

During warm-up:

- the Inbox reports incomplete and does not claim the ordering is globally
  complete;
- rows already indexed render normally, ordered normally;
- Manual Refresh does not trigger seeding at all;
- a webhook may seed or update any conversation immediately, and naturally takes
  precedence -- a row it has already written is not unseeded, so a batch will
  not read it again.

Coverage is complete when every Current+Upcoming ref has an activity row. Normal
rotating-sweep semantics begin from that point, with no separate mode flag: the
condition *is* "no unseeded refs remain".

A provider failure mid-warm-up preserves whatever was seeded and leaves the page
visibly incomplete. Progress is never lost and never overstated.

## What does not change

- **Historic webhook-surfaced rows** stay index-visible, order by the same
  column, and enrich through the existing shared-scan path.
- **Failed or empty provider reads never erase known-good activity.** The
  `_has_known_good_activity` / `_is_empty_summary` guards and the
  absence-means-preserve branch in `_enrich` continue to apply, unchanged.
- **No guest text and no provider identifiers** enter `conversation_activity`.
  The schema is untouched, so both invariants hold by construction.
- **Manual Refresh, the webhook fast path, and partial-failure handling** are
  unchanged.
- **Nothing sends.** `send_guest_reply` stays DANGEROUS behind human approval.

## Implementation invariants

These are the acceptance criteria, stated so they can be checked one by one
rather than inferred from the prose above.

1. `GET /inbox` **never reads every thread to derive ordering.** Ordering costs
   zero provider calls.
2. Ordering comes from `conversation_activity`, not from a live scan.
3. The Current+Upcoming booking scan exists for exactly two jobs: resolving refs
   to threads, and revealing conversations the index has not seen.
4. Only rows that make the page are re-read for display.
5. A new or unseeded Current+Upcoming ref is read once and indexed.
6. The recovery sweep selects the least-recently-refreshed `SWEEP_SIZE` rows.
7. A missed webhook is eventually recovered by the sweep, within one cycle.
8. Historic webhook-surfaced rows stay index-visible.
9. A failed or empty provider read never erases known-good activity.
10. No guest text and no provider identifier is added to
    `conversation_activity`.
11. Request counts are asserted by tests for: normal `GET /inbox`, a page with a
    Historic row, the recovery sweep, and cold start.
12. No single request ever performs a thread read for every conversation in the
    account. Cold start is bounded to `SEED_BATCH` per cycle.
13. Seeding happens only in the preparation cycle. `GET /inbox` and Manual
    Refresh never seed.
14. Unseeded refs are served before least-recently-refreshed sweep work.
15. While any Current+Upcoming ref is unseeded, the Inbox reports incomplete.

## Schema

**None.** Every column already exists, both required indexes already exist. This
is a read-path change with no migration.

## Testing

Request counts are the point of this change, so they are asserted, not assumed.
Required count assertions: normal `GET /inbox`; a page containing a Historic
row; the recovery sweep; and cold start.

Behavioural tests: ordering comes from the index with **zero** thread reads
performed for ordering; the page reads exactly the threads it displays; an
unseeded ref is seeded by exactly one read; the sweep re-reads only its slice
and selects the least-recently-refreshed rows; full coverage is reached within
one cycle; a missed webhook is recovered by the sweep; a page past the threshold
sets the stale flag and the console renders it; cold start seeds once and not
again; a Historic row still costs one shared archive scan; a 429 part-way
through degrades to a partial page rather than a 502; failed and empty reads
cannot overwrite known-good rows; and no guest text or provider identifier
reaches the index.

Cold-start tests: an empty index seeds only `SEED_BATCH`, not the account; the
Inbox is marked incomplete during warm-up; the next cycle seeds the next batch;
unseeded refs are preferred over stale indexed refs; coverage eventually
completes; the transition to normal rotating-sweep semantics happens once
nothing is unseeded; no single request performs a thread read per conversation;
a ref already upserted by a webhook is not redundantly seeded; and a provider
failure during warm-up preserves progress while remaining visibly incomplete.

All fixtures invented. No live provider or model calls in tests. No test touches
`agentops.db`.

## Live verification

Read-only, nothing sent. Measure `GET /inbox` and recovery-sweep request counts
against the real account and compare them to the targets above. Confirm the
index ordering matches the ordering a full scan would produce — if the index is
lying, that is the finding. Confirm a Historic row still surfaces. Observe one
sweep cycle. Watch a controlled window for 429s using cheap single-call probes
rather than repeated full loads, so verification does not reintroduce the load
being removed.

## Known limitations

- **Ordering can lag by up to one sweep cycle** for a conversation whose webhook
  was lost and which is not on the page. Visible through the stale flag, not
  silent, but real.
- **The cycle lengthens with the account.** At 25 per 120s, coverage time grows
  linearly with conversation count. Worth revisiting when the account roughly
  doubles.
- **The 3-call floor exists because `thread_uid` is not persisted.** That is a
  deliberate privacy choice, and this is its cost.
- **A brand-new database shows a partial Inbox for about twelve minutes.**
  Deliberate: bounded provider load was judged more important than a
  falsely-complete Inbox, and the incomplete flag makes the gap visible.
