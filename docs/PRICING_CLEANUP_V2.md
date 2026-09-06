# Pricing cleanup V2 — explicit expiry for temporary fixed-price writes

**Status: proposed. Not implemented. RAISE and LOWER remain blocked.**

This design replaces a dependency on PriceLabs' `lead_time_expiry` with an
expiry AgentGuard performs itself and can prove happened.

`EXPIRY_SEMANTICS_VERIFIED` is untouched by this document and stays `False`.
Nothing here unblocks a price write; a separate gate does that, and only after
this design is approved and tested.

---

## 1. Why not `lead_time_expiry`

The first live write sent `lead_time_expiry: 3`. PriceLabs accepted it and
echoes it back unchanged. That proves acceptance and persistence and nothing
else:

* no computed expiry date is returned,
* no status field says whether expiry is pending or done,
* nothing in the payload can be observed changing as the date approaches.

So the only way to learn what the field does is to wait and watch — which is
what the 2026-09-18 check does. Even a positive result there would leave the
mechanism *unobservable in the moment*: we would know it worked once, not that
it worked for a given override on a given day.

An explicit cleanup is strictly more auditable. Every removal becomes an
approval-linked audit event we can point at, rather than a silent lapse we
infer from an absence.

**This design stands whatever the 2026-09-18 check returns.** A positive result
would make `lead_time_expiry` a belt alongside these braces, not a replacement
for them.

---

## 2. The record

Every temporary fixed-price write creates one durable row, written **before**
the override is sent. A write with no row must be impossible: the row is what
makes cleanup owed, and an override nobody recorded is exactly the stranded pin
this feature exists to prevent.

| Field | Purpose |
|---|---|
| `id` | Primary key. |
| `listing_id`, `pms`, `stay_date` | Which night. |
| `old_price` | What was there before, so a reviewer can see what was displaced. Null when there was no override. |
| `new_price` | What we wrote. Half of the ownership proof. |
| `currency` | As sent. |
| `marker` | The AgentGuard ownership token written into `reason`. See §3. |
| `reason_sent` | The exact `reason` string sent, marker included, so the confirming re-read can be compared byte-for-byte. |
| `approval_id`, `run_id` | The human decision that authorised it. |
| `created_at` | When AgentGuard sent it. |
| `provider_created_at` | `created_at` as PriceLabs reported it on the confirming re-read. **Mandatory** for anything V2 wrote — see below. |
| `cleanup_at` | When the override must be removed. Explicit, not derived at read time. |
| `state` | See §4. |
| `resolved_at`, `resolution` | How it ended. |

`cleanup_at` is stored rather than computed so that changing the default policy
later cannot silently re-date overrides already in flight.

---

## 3. Proving the override is still ours

The hard requirement is **never delete a human-changed override**. PriceLabs
gives an override no id, so identity has to be carried in its contents.

### The ownership marker

Every V2 write puts an AgentGuard token at the **front** of `reason`:

```
AGENTGUARD:<cleanup_record_uuid>: <short human-readable reason>
```

The token is generated with the row, before the write, and stored on it. It is
what makes ownership a *match against one specific record* rather than an
inference from a sentence's uniqueness.

Relying on the natural-language reason alone was the design's weakest point.
Today it happens to be unique — of 55 live overrides, only the one AgentGuard
wrote has a populated `reason` — but that is a property of the current data,
not a guarantee. `reason` is free text; a person could one day type something
that collides, and the check would silently start matching an override we do
not own. A namespaced id cannot collide by accident.

The token goes first so that any provider-side truncation removes the
human-readable tail rather than the identity.

### `provider_created_at` is mandatory before a row may become ACTIVE

A row without one could only ever satisfy three of the four checks. Admitting
it to `ACTIVE` would create a record held to a quietly weaker standard than its
neighbours, with nothing downstream saying so.

So if the confirming re-read cannot supply a creation time, the row goes
straight to `NEEDS_REVIEW` with the override still in place, and `mark_active`
returns the state actually reached so a caller cannot assume success. Cleanup
refuses such a row too, as defence in depth.

The adopted pre-V2 row is exempt from the *marker*, not from this: it carries a
`provider_created_at` taken from the audit record.

### The four checks

Cleanup proceeds only when **all four** hold against the row:

| Check | Against |
|---|---|
| `reason` starts with `AGENTGUARD:<uuid>:` for **this row's** uuid | `marker` |
| `price` equals what we wrote | `new_price` |
| `created_at` equals what the confirming re-read reported | `provider_created_at` |
| `updated_at` equals `created_at` | — |

Any mismatch → `NEEDS_REVIEW`, and nothing is sent.

### Marker integrity is checked at write time, not at cleanup time

The observed `reason` that round-tripped intact was 106 characters. PriceLabs'
maximum length for the field is **not known**, and a truncated marker would
break ownership detection — silently, and only discovered a week later when
cleanup refused.

So the confirming re-read after every V2 write compares the returned `reason`
byte-for-byte against `reason_sent`. If it differs — truncated, normalised,
anything — the write is immediately `NEEDS_REVIEW` with the override still in
place and a person told, rather than left to fail at `cleanup_at`. That turns
an unverifiable provider assumption into an error caught within seconds of the
write that caused it.

The human-readable tail is capped so the whole string stays near the length
already proven to survive.

### The `updated_at` trap, stated plainly

It is tempting to treat `updated_at != created_at` as "a human edited it". That
inference is **unverified**: no edited override exists in the account, so we
have never observed PriceLabs bump `updated_at`, and it may not.

The design therefore does not depend on it. `updated_at` is checked as a
*positive* signal (it must still equal `created_at`), never as the sole detector
of tampering. With the marker in place the load-bearing check is the token,
which a human editing the date would have to reproduce exactly — including a
uuid they have never seen — for a false match.

This is the one assumption left worth tightening later, by deliberately editing
a test override and observing whether `updated_at` moves.

## 4. Lifecycle

```
                  approval granted
                        |
                        v
  (row written)  PENDING_WRITE
                        |
         write sent, re-read confirms
                        |
                        v
                     ACTIVE  ------------------------- cleanup_at reached
                        |                                      |
        override vanished before cleanup                       v
        (guest booked / human removed)              atomic claim (CAS)
                        |                            /               \
                        v                        won                lost
                    VANISHED                      |                   |
                                                  v                   v
                                              CLAIMED           do nothing
                                                  |             (no read,
                                          provider re-read       no write)
                                                  |
                                           ownership check
                                            /            \
                                       matches          differs
                                          |                 |
                                          v                 v
                              CAS: CLAIMED + my token   NEEDS_REVIEW
                                   -> DELETE_STARTED
                                    /            \
                                 won             lost
                                  |               |
                                  v               v
                            DELETE once     send nothing
                                  |         (OWNERSHIP_LOST)
                        re-read to confirm
                          /            \
                     absent           present
                        |                 |
                        v                 v
                   CLEANED_UP        NEEDS_REVIEW


  CLAIMED, lease expired, nobody released it
                        |
                        v
              reconciliation: read for diagnosis only
                        |
                        v
                  NEEDS_REVIEW          <-- never a second DELETE
```

Plus `UNKNOWN_CLEANUP_STATE` when the DELETE's outcome cannot be established —
never retried, always surfaced.

Terminal states: `CLEANED_UP`, `VANISHED`, `NEEDS_REVIEW`, `UNKNOWN_CLEANUP_STATE`.
`CLAIMED` and `DELETE_STARTED` are the two non-terminal states a row can sit in
during a pass. A row left in `DELETE_STARTED` is never resolved by automation
and waits for a person.

---

## 4a. Concurrency: one obligation, at most one automatic DELETE

Two things drive the runner — `POST /pricing/cleanup/run` and the hourly
`app.pricing_cleanup_job` — and neither knows about the other. They can run at
the same moment, in different processes, on different hosts. Without
coordination both would select the same due row, both would prove ownership
against the same override, and both would send a DELETE. The second one is the
dangerous half: if a person re-pinned that date in between, it destroys their
pin.

**`due()` is not permission.** It returns a candidate set, and two callers get
the same list. That is expected and safe, because selecting a row grants
nothing.

**The claim is the gate, and it is one atomic `UPDATE`.**

```sql
UPDATE pricing_cleanups
   SET state='CLAIMED', claim_token=?, claimed_at=?, lease_until=?
 WHERE id = ? AND state='ACTIVE'
```

The `WHERE` is the compare, the `SET` is the swap. Concurrent callers both
reach the database and exactly one matches a row; the loser sees `rowcount = 0`
and does nothing. This is durable state, not a Python lock, a module global or
a single-instance assumption — it holds across processes, hosts and any future
AWS instances.

**The claim precedes the provider read, not the DELETE.** A loser therefore
never reads, so two processes can never simultaneously hold an ownership proof
for the same override.

### The delete boundary: `DELETE_STARTED`

A claim authorises *work*. It does not authorise the *call*.

The claim alone leaves one hazard open. A process can claim a row, read the
provider, prove ownership, then stall past its lease. Reconciliation settles
the row to `NEEDS_REVIEW`, a person starts acting on it — and then the stalled
process resumes straight into `remove_override` and deletes an override
somebody had already taken over. Its token would stop the *verdict* from
landing, but only after the provider call had happened.

So there is a second compare-and-swap, immediately before the call:

```sql
UPDATE pricing_cleanups
   SET state='DELETE_STARTED'
 WHERE id = ? AND state='CLAIMED' AND claim_token = ?
```

`remove_override` is called **only if that CAS committed**. Reconciliation
clears `claim_token`, so a row it settled fails this on both predicates and
nothing is sent. The stale process reports `OWNERSHIP_LOST`, emits
`PRICING_CLEANUP_STALE_OWNER` with `provider_delete_attempted: false`, and
changes nothing.

Everything above that line is reversible. Everything below it may reach a third
party.

The kill switches are checked *before* the boundary is taken, so a pass that
cannot write puts the claim down and leaves the row `ACTIVE` rather than
parking it at a boundary that would need a person.

### A row left at the boundary belongs to a person

`DELETE_STARTED` is an **ambiguous external-write boundary**. A process that
died there may have sent its DELETE or may not, and neither system can settle
which — the same reason a lease cannot make the call exactly-once. So:

- `due()` selects `ACTIVE` only, and `expired_claims()` selects `CLAIMED` only,
  so no automatic pass ever picks up a `DELETE_STARTED` row;
- reconciliation never changes one, whatever the provider now shows;
- a later pass may read the provider for diagnosis, but that reading is never
  permission for a second automatic DELETE;
- `open_records()` and `overdue()` both include it, because a row automation
  will never resolve is only ever seen if it is surfaced.

Once the boundary is committed, the holder records the terminal state using the
same token — nothing can take the claim away from a `DELETE_STARTED` row, since
reconciliation only touches `CLAIMED`.

### Operational rule: a `DELETE_STARTED` row is hands off

**`DELETE_STARTED` means an external DELETE may be in flight, or may already
have been sent.** Nothing in AgentGuard can tell which, and nothing at
PriceLabs will say.

While a row is in that state:

- **AgentGuard must never automatically retry it.** No pass, no schedule, no
  backfill.
- **AgentGuard must never offer an automatic cleanup or retry control for it.**
  Not a button, not a menu item, not an API parameter. (There is no such
  control today: `POST /pricing/cleanup/run` takes no input, so nothing can be
  aimed at a specific row, and `GET /pricing/cleanup` — see
  [The workload view](#the-workload-view) — only reads.)
- **A human reviewing a stranded `DELETE_STARTED` record must treat that stay
  date as "hands off"** until the original worker or request is known to have
  finished. Editing the date at the provider while a DELETE may still land is
  the collision this whole design exists to avoid.
- **Human review may read PriceLabs for diagnosis.** Reading is always safe;
  it just cannot settle the question.
- **No automated action may infer from elapsed time that another DELETE is
  safe.** Not after an hour, not after a week. Elapsed time is not evidence
  about an external side effect.

This is an unavoidable boundary, not a gap someone forgot to close. Our
database transaction cannot include the PriceLabs DELETE, and it cannot exclude
a person editing the same override in the PriceLabs UI at the same moment.
Exactly-once is therefore not available at any lease length or retry policy.
What *is* available is never doing it twice automatically, and that is what
`DELETE_STARTED` buys.

### Expired claims fail closed

A claim carries `lease_until`. When it lapses without being released, the row
is **not** taken over and retried. It is reconciled to `NEEDS_REVIEW` with zero
writes.

The reason is that a PriceLabs DELETE cannot participate in our database
transaction. There is no shared commit between the two systems, so when a claim
expires we cannot distinguish a process that:

- died before sending its DELETE,
- is frozen on the line immediately before sending it,
- sent it and died before recording the result, or
- is simply slow and will resume.

**No amount of elapsed time separates those.** A lease can bound a window; it
can never establish that an external side effect did not happen. So exactly-once
delete cannot be guaranteed by a lease, and a longer lease would not change
that — lengthening it buys nothing and is not the fix.

Fifteen minutes (`CLAIM_LEASE_SECONDS`) is therefore a threshold for calling a
claim *stale*, not a licence to take it over. Stale means review.

Reconciliation may read the provider, purely to describe what a person will
find, and records that in the resolution: the override is still in place, it is
already gone, or PriceLabs could not be reached. It never writes.

`release()` is a different thing and is deliberately allowed to return a row to
`ACTIVE`: it is the live owner putting the row down on a path where **nothing
was sent** — the provider could not be read, or the kill switch is off. We know
no DELETE was attempted, so the row may safely re-enter the automatic path.

### Why this asymmetry

A stranded AgentGuard override costs a person a few minutes clearing a queue.
Deleting a pricing decision a person made in the meantime cannot be undone.
`NEEDS_REVIEW` is preferred over any retry that could be destructive, and that
preference is the design, not a limitation of it.

`resolve()` takes an `expected_token` so a process whose claim lapsed cannot
overwrite the verdict reconciliation already recorded. A settled row clears
`claim_token` and `lease_until`, so terminal never looks like work in progress.

### The audit reports what committed, not what was concluded

A refused write is not the end of it: the process that lost its claim must not
*say* it settled the row either. An event announcing `CLEANED_UP` for a row the
database never moved would make the trail disagree with the thing it exists to
describe.

So a refused resolution emits **`PRICING_CLEANUP_STALE_OWNER`** instead of
`PRICING_CLEANUP`, carrying `attempted_state`, `provider_delete_attempted`, the
claim token, the detail, and the originating `approval_id` / `run_id`. The
DELETE attempt is preserved deliberately — that side effect is real whatever
happened to our bookkeeping afterwards, and it is exactly what someone
reconstructing the night needs.

The returned outcome reports `OWNERSHIP_LOST` rather than the state it wanted,
and `summarise()` counts it that way, so a route response never reports a
terminal state the row does not have. The current state is *not* substituted in
its place: this process has not read the row back and will not guess.

### `CLAIMED` implies a claim token

`claim` is the only writer of `CLAIMED` and always sets a token, so a CLAIMED
row without one is an invariant violation. If one ever appears, reconciliation
still sends nothing to the provider and settles it through
`resolve_unclaimed()` — its own compare-and-swap on
`state='CLAIMED' AND claim_token IS NULL`, never an unconditional write — with
the violation named in the resolution and the audit event. A properly claimed
row does not match that predicate and is left alone.

---

## 5. The cleanup procedure

For each row where `state = ACTIVE` and `cleanup_at <= now`:

1. **Re-read** the override for that listing and date.
2. **Absent?** → `VANISHED`. Nothing to do; the date is already back on dynamic
   pricing. Not a failure.
3. **Present but not ours** (any of the four checks fails) → `NEEDS_REVIEW`.
   **Send nothing.** Record what differed, so a person can see whether their own
   change is now sitting under our record.
4. **Present and ours** → send exactly one DELETE.
5. **Re-read.** Absent → `CLEANED_UP`. Still present → `NEEDS_REVIEW`.
6. **DELETE outcome unknown** (timeout, transport failure, unreadable re-read)
   → `UNKNOWN_CLEANUP_STATE`. **Never retried automatically.** The override may
   already be gone; a second DELETE against a date a human has since re-pinned
   would destroy their work.

Overdue rows — `cleanup_at` in the past and still `ACTIVE` — are a first-class
condition, not an absence of one. They surface on `/vacancy` whether or not the
runner has executed, because "cleanup did not happen" must be visible without
depending on the thing that failed.

---

## 6. What runs it, and why it needs no per-run approval

Cleanup is **restorative**: it returns a date to the state it had before an
approved change, within limits a human already approved. Not running it is the
dangerous outcome. Requiring a fresh approval per cleanup would mean an
unapproved cleanup leaves a permanent pin — the failure mode inverted.

So cleanup runs without a new approval, but only under conditions that are
narrower than any other write in the system:

* it only ever **removes**, never sets a price;
* it only touches a date named in a row AgentGuard itself wrote;
* it refuses unless ownership is proven against four recorded fields;
* it is bounded by the row — it cannot discover work for itself.

It stays behind `ENABLE_PRICING_WRITES` and the per-listing allowlist, and
every action is audited with its originating `approval_id`, so each cleanup is
traceable to the human decision that created the obligation.

**Trigger:** a `POST /pricing/cleanup/run` route, called by the same scheduled
cloud routine mechanism already used for the 2026-09-18 check. No in-process
scheduler, no background thread — AgentGuard stays request-driven, and the
trigger is inspectable and repeatable by hand.

### The workload view

`GET /pricing/cleanup` reports what cleanup owes, so an administrator can see
the queue before deciding to run a pass: `PENDING_WRITE` / `ACTIVE` /
`CLAIMED` / `DELETE_STARTED` / `NEEDS_REVIEW` counts, how many rows are due
now, the age of the oldest overdue obligation, and the rows automation will
never resolve on its own.

Four properties keep it from becoming a control surface.

* **It is a separate GET, not a dry-run flag on the POST.** A parameter that
  switches between "report" and "act" is one typo away from acting.
* **It takes no input.** No id, listing or date — the same reason the POST
  takes none. It cannot be used to hunt for one night's record.
* **It mutates nothing.** It does not claim, reconcile, expire a lease, or
  return a row to the queue. Reading an expired claim through this view leaves
  it `CLAIMED` for `expired_claims` to fail closed on, exactly as before.
  Tests assert the whole store is byte-identical across a read.
* **It carries no claim token and no lease.** An operator needs to know a row
  is stuck, not to be handed the fence that is holding it.

`due_now` is asserted in tests against `due()` itself rather than re-derived,
so the number shown before a manual run cannot drift from the candidate set
that run will attempt. It remains a candidate count: `claim` is still the gate.

**Any role may read it.** Running a pass still requires `ADMINISTER`; that
separation — everyone may see, few may cause — is the point.

---

## 7. What this does not change

* `LOWER` and `RAISE` stay blocked.
* **`CLEANUP_STRATEGY_VERIFIED` is the sole unlock.** It is set only after this
  design is implemented, unit-tested, and exercised live end to end: write →
  active → cleanup → confirmed removal.
* `EXPIRY_SEMANTICS_VERIFIED` is **informational only and is not a permission**.
  It was briefly an alternate unlock and no longer is. Provider-side expiry is
  unowned, unobservable in the moment, and leaves no per-override audit trail;
  even proven it would show the mechanism worked once, not that it worked for a
  given override on a given day. A positive result is a second belt, never the
  braces.
* Nothing here is unattended pricing. A price still moves only when a human
  approves that exact change.

---

## 8. Decisions — settled 2026-09-05

1. **Default `cleanup_at` = `min(stay_date - 2 days, created_at + 7 days)`.**
   The dual bound is the point: an override cannot linger more than a week, and
   is always gone before the last two days before arrival. Not to be made
   cleverer in V1.
2. **Cadence: hourly.** Cleanup is not latency-sensitive at a date scale, and an
   hourly runner is easy to reason about.
3. **A booked night is still owed a cleanup.** The override is economically
   moot while the reservation stands, but it is technically still stranded: if
   the booking cancels, that fixed price becomes live again.
4. **The existing 2026-09-21 override is adopted — manually and explicitly.**
   No silent backfill. A single adoption path writes one row from the exact
   audit record and live provider state, marks it `ADOPTED`, and records that it
   predates V2. The normal lifecycle owns it from there.

   That override was written **without** a marker, so the token check cannot
   apply to it. Its row records this, and ownership for that one row falls back
   to price + `created_at` + `updated_at` against the audit record. It is the
   only row that will ever be exempt from the marker check, and the exemption is
   stored on the row rather than inferred.

---

## 9. Test plan

Unit, with invented data:

* a write with no row is impossible
* `cleanup_at` is stored, not recomputed
* the marker is generated per row and written at the front of `reason`
* a write whose confirming re-read returns a different `reason` becomes
  `NEEDS_REVIEW` immediately, without waiting for `cleanup_at`
* ownership passes when all four checks match
* ownership fails on a changed price / missing or altered marker / a marker
  belonging to a different row / changed `created_at`
* the adopted pre-V2 row is exempt from the marker check and nothing else is
* a failed ownership check sends **zero** requests
* absent override → `VANISHED`, zero requests
* successful path → exactly one DELETE, re-read confirms, `CLEANED_UP`
* re-read still present after DELETE → `NEEDS_REVIEW`
* unknown outcome → `UNKNOWN_CLEANUP_STATE`, and a second run does **not** retry
* an overdue row surfaces even when the runner never ran
* cleanup respects both kill switches
* the model cannot invoke cleanup

For the workload view, the counts matter less than the property that reading
changes nothing — so those tests snapshot every open row's state, claim token,
lease and resolution across a read, in each state a reader might plausibly be
tempted to reconcile.

Live, once unit tests pass: one temporary write on a low-risk date with a short
`cleanup_at`, then observe the full lifecycle through to confirmed removal.
**Both live proofs have now been run and are recorded in
[PRICING_LIVE_PROOFS.md](PRICING_LIVE_PROOFS.md)** — RAISE on 2026-09-05, LOWER
on 2026-09-06, the latter with full evidence including the row-before-write
ordering, the `DELETE_STARTED` boundary, single-DELETE terminality and audit
linkage back to the originating approval.

---

## 10. Cost of being wrong

If ownership detection is too loose, AgentGuard deletes a human's pricing
decision. If too strict, cleanups pile up in `NEEDS_REVIEW` and someone has to
clear them by hand.

The second failure is recoverable and visible; the first is neither. Every
ambiguous case in this design therefore resolves to `NEEDS_REVIEW`.
