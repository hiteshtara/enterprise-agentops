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
| `reason_marker` | The exact `reason` string we sent. The other half of the ownership proof — see §3. |
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
gives an override no id, so identity has to be reconstructed from its contents.

Observed on 2026-09-05 across all 55 live overrides:

| Field | What it gives us |
|---|---|
| `reason` | **Ours is the only populated one.** All 55 human-created overrides carry `reason: ""`; the one AgentGuard wrote carries its full sentence. Strong and, in practice, unique. |
| `price` | Must equal `new_price`. |
| `created_at` | Must equal `provider_created_at`. |
| `updated_at` | Equals `created_at` on every observed row. |

**Ownership holds only when all four match the record.** Any mismatch means
someone or something else has touched that date, and cleanup must not proceed.

### The `updated_at` trap, stated plainly

It is tempting to treat `updated_at != created_at` as "a human edited it". That
inference is **unverified**: no edited override exists in the account, so we
have never observed PriceLabs bump `updated_at`, and it may not.

The design therefore does not depend on it. `updated_at` is checked as a
*positive* signal (it must still equal `created_at`), never as the sole detector
of tampering. The load-bearing checks are `reason` and `price`, which a human
editing the date would almost certainly change — a human setting an identical
price *and* retyping our generated sentence verbatim is not a failure mode
worth designing around, and if it happened, deleting the override would restore
the same dynamic pricing they were reaching for anyway.

This is the one assumption in the design that could be tightened later, by
deliberately editing a test override and observing whether `updated_at` moves.

---

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

## 8. Decisions needed before implementation

1. **Default `cleanup_at`.** Proposal: `min(stay_date - 2 days, created_at + 7
   days)`. Short enough that an unattended override cannot ride into arrival,
   long enough to give the price time to work. Both terms are arbitrary until
   you set them.
2. **Cadence.** Proposal: hourly. Finer buys little, since `cleanup_at` is a
   date-scale decision.
3. **Does an override still count as owed once the night is booked?**
   Proposal: yes, clean it up anyway. The price is moot for that reservation,
   but leaving it strands a pin if the booking is later cancelled.
4. **Should cleanup extend to the 2026-09-21 override already live?** It was
   written before this design, so it has no row. Proposal: adopt it manually by
   writing a row from the audit record, rather than leaving it to
   `lead_time_expiry` alone.

---

## 9. Test plan

Unit, with invented data:

* a write with no row is impossible
* `cleanup_at` is stored, not recomputed
* ownership passes when all four fields match
* ownership fails on a changed price / changed reason / changed `created_at`
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
