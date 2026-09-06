# Live pricing proofs

What AgentGuard has actually done to a real PriceLabs account, under human
approval, with the evidence that it did it correctly.

A green test suite proves the code does what the tests say. It does not prove
the provider accepted the write, that the override we read back is the one we
wrote, or that the cleanup we owe ever ran. Those are properties of a live
system, and the only way to establish them is to do the thing once, under
control, and check. This file is the record of those occasions.

**Rules for anything recorded here.**

- One action per proof, approved by a person out-of-band, never self-approved.
- Every claim is backed by an independent re-read, not by the write's own
  response.
- Instrumentation errors are recorded alongside the result. A proof that
  overstates its own rigour is worse than no proof.
- Nothing here authorises a flag change. Verification flags describe knowledge
  about the world; a successful proof describes AgentGuard's behaviour. They
  are different claims (see [Flag boundaries](#flag-boundaries)).

---

## 2026-09-06 — first live LOWER: Boston Bunkers, 2026-09-10

**Verdict: PASS.**

| | |
|---|---|
| Property | Boston Bunkers (`680444___747423`) |
| Stay date | 2026-09-10 (4 days out) |
| Action | LOWER `$182 → $164`, −$18 / −9.89% |
| Approval | `62b7f7da-1e26-4fc0-8f34-ed70511f871e` |
| Run | `67bc1498-7a66-4ecc-8e5e-4de41634a2bf` |
| Cleanup obligation | `925c2dae-75a2-4e62-8e42-cf2e95d48968` |
| Fingerprint | `7682947edd62f8ef` |

### The lifecycle, as observed

```
fresh provider state
  → recommendation
  → human approval (out-of-band)
  → execution-time fingerprint match
  → PENDING_WRITE cleanup obligation created FIRST
  → exactly one PriceLabs POST
  → independent re-read: fixed $164 override present
  → cleanup row ACTIVE, carrying approval_id and run_id
  → production cleanup job
  → CLAIMED
  → DELETE_STARTED boundary committed
  → exactly one DELETE for this obligation
  → independent re-read: override absent
  → CLEANED_UP
  → second pass: zero work
```

### Evidence

**Row before write.** The store was snapshotted from inside the write call,
before it reached PriceLabs:

```
POST ('680444___747423', 'lodgify', '2026-09-10', 164.0)  automation_enabled=True
rows at call: [('925c2dae…', 'PENDING_WRITE', …, approval 62b7f7da…, run 67bc1498…)]
```

The obligation existed, already carrying both identifiers, before the
irreversible call began. This is the `record_intent`-then-write ordering that
[PRICING_CLEANUP_V2.md](PRICING_CLEANUP_V2.md) exists to guarantee, observed
rather than assumed.

**Provider confirmation**, by re-reading rather than trusting the response:

```json
{"date": "2026-09-10", "price": "164", "price_type": "fixed", "currency": "USD",
 "reason": "AGENTGUARD:925c2dae-75a2-4e62-8e42-cf2e95d48968: 4d out and still open; …",
 "created_at": "2026-09-06T23:21:45.000Z",
 "updated_at": "2026-09-06T23:21:45.000Z"}
```

`updated_at == created_at` — one write, not two. The marker resolves to the
cleanup row; the `reason` is byte-for-byte what AgentGuard recorded as sent.

**The delete boundary**, snapshotted at the moment of the provider DELETE:

```
DELETE ('680444___747423', 'lodgify', '2026-09-10')
state at call: DELETE_STARTED
claim token  : 3e867f6d-…  claimed 23:23:38Z  lease until 23:38:38Z
```

The fence was committed before the irreversible call, under a held claim.
Terminal row: `CLEANED_UP`, `claim_token=None`, `lease_until=None`, both
identifiers retained.

**Audit linkage** — the gap the earlier RAISE proof exposed, now closed:

```
23:16:11  TOOL_REQUESTED     run 67bc1498…
23:16:11  APPROVAL_REQUIRED  approval 62b7f7da…
23:21:39  APPROVAL_GRANTED   approval 62b7f7da…
23:21:45  TOOL_EXECUTED      run 67bc1498…
23:23:40  PRICING_CLEANUP    approval 62b7f7da… · run 67bc1498… · cleanup 925c2dae…
```

The `PRICING_CLEANUP` event carries the **originating** approval, not just the
cleanup's own id.

**Neighbour safety.** 11 other Bunkers overrides, before and after, identical
in price, type, reason and `updated_at` — every one of them stamped before the
POST at `23:21:45Z`.

**Final provider state.** Override absent, published price back to $182,
`booking_status=''`, `unbookable=0`. The night did not sell during the
~2-minute window, which is expected: this was a lifecycle proof, not a pricing
campaign.

### A stale approval was correctly refused first

The first approval for this night (`39296084-…`, fingerprint `e00a646b5ae270ad`)
was created at 22:55Z. By 23:04Z the comp set's booked median had moved **$232
→ $233** — a single hashed field — and the fingerprint became
`7682947edd62f8ef`. Pre-flight stopped; the approval was rejected as stale and
its run cancelled, producing **zero** pricing writes. A replacement was created
only after re-verifying every condition against fresh state.

That the drift was one dollar is the point. `fingerprint()` hashes
`current_price`, `market_p25`, `market_booked_median`, `pinned_price` and
`demand`; what counts as material is a property of that function, and it
declared this material. The stale-decision guard is not decoration.

### Instrumentation errors, recorded honestly

Two mistakes in the *proof harness*, not the production code. Both are recorded
because a proof that hides its own defects cannot be relied on later.

1. **A negative guard that could not fire.** During the rejection and
   approval-creation steps, the harness patched `delete_override` to raise. The
   production method is **`remove_override`**, so the patch created a stray
   attribute and shadowed nothing. The "zero DELETEs" claim for those steps was
   therefore unproven rather than false — no cleanup runner ran, so no DELETE
   path was structurally invoked, but the guard did not establish that. The
   `set_override` guard in the same steps *was* correctly named and did hold.

2. **A globally-scoped assertion.** The first Phase B check counted every
   DELETE the pass sent and failed at four. The production runner had correctly
   processed **all five due obligations** — four pre-existing rows from earlier
   demo work, plus the one under test. Re-scoped to obligation `925c2dae…`,
   exactly one DELETE was sent.

The four incidental rows (`dd2cb5dd` VANISHED; `1a45ea0a`, `ae26a2ad`,
`efa3a986` CLEANED_UP) were AgentGuard's own expired overrides on other
listings, returned to dynamic pricing. No condo 2nd-Floor row was touched.

**The worker was not changed in response.** Processing every due obligation is
correct behaviour. A "clean up only this row" mode would be a way to aim the
irreversible path at a night of someone's choosing, which is exactly what
`POST /pricing/cleanup/run` takes no input in order to prevent. The proof was
re-scoped; the worker was not.

---

## Flag boundaries

A live proof establishes what AgentGuard **does**. It never establishes a fact
about the outside world, and must not be used to flip a flag that asserts one.

| Flag | Still | Why a passing proof does not move it |
|---|---|---|
| `BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED` | `False` | Describes what we know about Booking.com's stacked promotional/Genius discounting, not whether LOWER executes safely. The maximum effective guest discount remains unknown. |
| `EXPIRY_SEMANTICS_VERIFIED` | `False` | Concerns what PriceLabs' own `lead_time_expiry` means. Unrelated; AgentGuard removes what it wrote regardless. |
| `ONE_NIGHT_STAYS_ALLOWED` | `False` | An owner policy, not a capability. |
| `OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE` | `True` | An authorization the owner gave under stated uncertainty. It is not a verification and must never be renamed as one. |
| `CLEANUP_STRATEGY_VERIFIED` | `True` | Set earlier, after the RAISE lifecycle proof. |

No maximum Booking.com discount has been derived, and none may be inferred from
a proof recorded here.

---

## Earlier: RAISE lifecycle proof (Arboretum, 2026-10-05)

The first live write proof — RAISE `$250 → $260` — was run on 2026-09-05 and
was **not written up at the time**. It is noted here for completeness rather
than reconstructed: its durable artefacts are the audit trail and cleanup rows
in the database, not this file. It is the proof that exposed the missing
`approval_id` / `run_id` linkage on cleanup rows, fixed by `ExecutionContext`
and confirmed closed by the LOWER proof above.
