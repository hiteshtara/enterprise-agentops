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
| `provider_created_at` | `created_at` as PriceLabs reported it on the confirming re-read. |
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
        (guest booked / human removed)                  ownership check
                        |                                /            \
                        v                          matches          differs
                    VANISHED                          |                 |
                                                      v                 v
                                                 DELETE once       NEEDS_REVIEW
                                                      |
                                            re-read to confirm
                                              /            \
                                         absent           present
                                            |                 |
                                            v                 v
                                       CLEANED_UP        NEEDS_REVIEW
```

Plus `UNKNOWN_CLEANUP_STATE` when the DELETE's outcome cannot be established —
never retried, always surfaced.

Terminal states: `CLEANED_UP`, `VANISHED`, `NEEDS_REVIEW`, `UNKNOWN_CLEANUP_STATE`.

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

---

## 7. What this does not change

* `EXPIRY_SEMANTICS_VERIFIED` stays `False`.
* `LOWER` and `RAISE` stay blocked.
* Unblocking them requires a **separate** flag, `CLEANUP_STRATEGY_VERIFIED`,
  set only after this design is approved, implemented, unit-tested, and
  exercised live end to end: write → active → cleanup → confirmed removal.
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

Live, once unit tests pass: one temporary write on a low-risk date with a short
`cleanup_at`, then observe the full lifecycle through to confirmed removal.

---

## 10. Cost of being wrong

If ownership detection is too loose, AgentGuard deletes a human's pricing
decision. If too strict, cleanups pile up in `NEEDS_REVIEW` and someone has to
clear them by hand.

The second failure is recoverable and visible; the first is neither. Every
ambiguous case in this design therefore resolves to `NEEDS_REVIEW`.
