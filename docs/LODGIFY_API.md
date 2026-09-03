# Lodgify API — AgentGuard engineering reference

Authoritative reference for AgentGuard's Lodgify integration. Everything here is
either read directly from Lodgify's own responses against the live account, read
from Lodgify's published documentation, or explicitly marked as inference.

**Nothing in this document is guessed.** Every claim carries an evidence level.

## Evidence levels

| Level | Meaning |
|---|---|
| **DOCUMENTED BY LODGIFY** | Stated in Lodgify's own published API reference. Not independently exercised by AgentGuard. |
| **VERIFIED LIVE** | AgentGuard issued a real request against the live account and observed the response. Dated. |
| **DOCUMENTED + VERIFIED LIVE** | Both of the above agree. |
| **OBSERVED BUT NOT YET VERIFIED ACROSS OTA CHANNELS** | Seen in live data, but only on one channel/route. Behaviour on Airbnb / Booking.com / Vrbo threads is not established. |
| **LIKELY / NOT YET VERIFIED** | Strongly suggested by secondary material (Lodgify's own product surfaces, integration partners). Not confirmed against an authoritative Lodgify source. |
| **INFERENCE / FUTURE WORK** | AgentGuard's reasoning or a design decision. Not an API fact. |

An inference is never presented as a verified API fact. Where a section mixes
levels, each claim is labelled inline.

---

## 1. Purpose

Lodgify is AgentGuard's **first real external business connector**. It currently
serves one tenant, **Priyanka Homes** (short-term rental operator, Boston).

Two things this integration is *not*:

- **AgentGuard does not depend at runtime on the Priyanka Homes application.**
  Priyanka Homes is a separate TypeScript/Next.js website. AgentGuard has its own
  Python connector (`app/connectors/lodgify/`) that talks to `api.lodgify.com`
  directly. There is no shared process, no shared database, no import, no HTTP
  call between the two systems.
- **The Priyanka Homes repository is a knowledge source, not a dependency.** Its
  `docs/LODGIFY_API.md` recorded previously verified parameter spellings, the
  property/room-type mapping, and the outcome of an earlier controlled write test.
  Those facts were copied into AgentGuard's configuration as *configuration*, and
  re-verified where this document says so.

*Evidence: INFERENCE / FUTURE WORK (architecture decision) — recorded here so the
boundary is not eroded by a later change.*

---

## 2. Authentication

**Base URL:** `https://api.lodgify.com`

**Authentication:** a single request header, `X-ApiKey: <key>`. No OAuth, no
bearer token, no cookie, no CSRF token. The key is account-scoped.

**AgentGuard configuration:** the environment variable `LODGIFY_API_KEY`,
resolved by exactly one function (`app/connectors/lodgify/config.resolve_api_key`).

*Evidence: DOCUMENTED + VERIFIED LIVE. Lodgify's published `securitySchemes`
declares `apiKey` in header `X-ApiKey`; every live call made during investigation
authenticated this way.*

### Security invariants

The credential:

- is **server-side only** — it never reaches the browser or any client bundle;
- is **never exposed to the model** — it is not a tool argument, not in a tool
  result, not in a tool description;
- is **never placed in tool arguments**, so it cannot reach a `RunStep`, an
  approval record, or a replayed conversation;
- is **never written to the audit log**;
- is **never written to observability** (`ToolExecution` / `ModelExecution`);
- is **never committed** — it lives only in a gitignored `.env` and in deployment
  environment variables;
- has **no fallback value**. An unset key makes the connector unavailable and its
  tools absent from the registry, rather than producing fabricated results.

It is resolved per call through the single resolver and never stored on a client
instance. Importing the application does not read it.

**No key, or any fragment of one, appears in this document.**

*Evidence: INFERENCE / FUTURE WORK (AgentGuard policy) + enforced by existing
tests in `tests/test_lodgify_connector.py`.*

---

## 3. Property configuration

Seven Priyanka Homes properties are Lodgify-backed. The mapping is *configuration*
copied from the audited Priyanka Homes repository and re-verified live against
`GET /v2/properties` and `GET /v2/properties/{id}`.

| Slug | `property_id` | `room_type_id` |
|---|---|---|
| `renovated-3rd-floor-retreat-3-beds-roslindale-village` | 680420 | 747399 |
| `renovated-2nd-floor-home` | 680434 | 747413 |
| `budget-friendly-basement-2br-retreat` | 680444 | 747423 |
| `modern-condo-walk-out-basement-near-train` | 680447 | 747426 |
| `boston-hospitality-homes-harvard` | 681286 | 748333 |
| `boston-condo-second-floor` | 681293 | 748340 |
| `arboretum-retreat-city-of-boston` | 681301 | 748348 |

**`south-boston-seaside-residence` is standalone and NOT Lodgify-backed.** It is
inquiry-only, has no Lodgify property id, and carries no provider identifiers at
all. It appears in `list_properties` as `lodgify_connected: false` and is absent
from the slug enum, so it cannot be queried through Lodgify even by mistake.

### The slug boundary

**Model-facing tools accept a `property_slug` from a closed enum. Provider ids are
resolved server-side.** A hallucinated or hand-crafted numeric id has no path to
the API, because no tool schema has a field that would carry one.

This is the general rule, not a property-specific one — see §6 and §10 for the
same boundary applied to bookings and threads.

*Evidence: DOCUMENTED + VERIFIED LIVE (mapping verified 2026-08-19 by the Priyanka
Homes project against all 7 properties; ids and names only, no booking data).*

---

## 4. Availability API

**`GET /v2/availability/{property_id}`**

| Parameter | Value |
|---|---|
| `start` | `YYYY-MM-DD`, inclusive |
| `end` | `YYYY-MM-DD`, inclusive |
| `includeDetails` | `true` |

### Verified parameter rule — load-bearing

**Use `start` / `end`. Never `from` / `to`.**

`from`/`to` are **silently accepted and ignored**. They do not error. The response
becomes parameter-invariant placeholder data — the same
`{"start":"0001-01-01","end":"0001-01-01","available":1}` regardless of property
or date range. This is the worst possible failure mode: a wrong answer that looks
like a right one.

*Evidence: VERIFIED LIVE, 2026-08-19, re-tested on two different properties, both
returning the identical placeholder.*

### AgentGuard's sanitized output

Only three fields per period are read: `start`, `end`, `available`. Booking rows,
channel calendars and guest data present on the real response are **dropped at
construction**, not filtered downstream.

`end` is Lodgify's own inclusive convention — the last night in that state. A
stay's departure date is the first night of the *next* period, so a checkout day
is available for a same-day check-in.

### Fail-closed semantics

A provider timeout, transport error, non-2xx status, unreadable body, or
unexpected shape is **not** "available". It becomes `unknown`:

```json
{"ok": false, "status": "unknown", "reason": "provider_unavailable",
 "message": "Availability could not be confirmed: ..."}
```

**An unknown result contains no `available` key anywhere.** It is structurally
impossible to misread a provider failure as an open calendar. Three outcomes stay
distinct: `ok: true` (an answer), `status: "declined"` (a provider booking rule
said no), `status: "unknown"` (no answer was obtained).

*Evidence: DOCUMENTED + VERIFIED LIVE (endpoint); INFERENCE / FUTURE WORK
(AgentGuard's fail-closed result shape, enforced by test).*

---

## 5. Quote API

**`GET /v2/quote/{property_id}`** — a **read-only pricing calculation**. It does
not create or modify a reservation.

| Parameter | Notes |
|---|---|
| `arrival` | `YYYY-MM-DD`. Not `from`. |
| `departure` | `YYYY-MM-DD`. Not `to`. |
| `roomTypes[0].Id` | The room type id from §3. |
| `roomTypes[0].guestbreakdown.adults` | **Exact spelling.** |

### `guestbreakdown` is exact

**Do not substitute an underscored variant.** `guest_breakdown` does not return a
clean 400 — it returns `500 {"message":"Object reference not set to an instance of
an object."}`. Also available: `.children`, `.infants`, `.pets`.

Using `from`/`to` here returns `400 {"message":"Invalid dates."}` — unlike
availability (§4), the quote endpoint does reject them.

A stay under the room's minimum returns a specific business-rule error, e.g.
`400 {"message":"The minimum stay for this rental is 2 days","code":666}`, so a
caller can distinguish "wrong request shape" from "valid request, breaks a rule".

### Safe returned fields

AgentGuard emits only:

`currency`, `accommodation_amount`, `cleaning_fee`, `taxes`, `total`.

The real response also contains cancellation-policy text, scheduled payments,
security deposit, rental agreement text, promotions and identifiers. **None of it
is forwarded.** Raw provider payloads are never returned.

**Read-only confirmation:** the response carries no booking id, reservation id or
quote id, and a cross-check of `GET /v2/reservations/bookings` around the test
window found zero bookings created or changed.

*Evidence: DOCUMENTED + VERIFIED LIVE. Parameter spellings and response contents
confirmed by Lodgify Support (ticket #1272548) and re-verified live 2026-08-19.*

---

## 6. Reservation / booking discovery

**`GET /v2/reservations/bookings`** — paginated (`page`, `size`, `includeCount`).
Returns `{count, items[]}`.

This is the supported capability used to reach conversations, because there is no
thread-list endpoint (§8).

### Verified structural findings

Booking objects carry a **`thread_uid`** field.

**All 143 bookings in the account had a `thread_uid`** at the time of inspection.
No booking was found without one.

Observed `source` values (the channel a booking arrived through):

```
AirbnbIntegration
BookingCom
HomeAway          (Vrbo; source_text reads "VRBO")
Manual
OH
PublicApi
```

`source_text` is a **free-text sibling** of `source` and is *not* a safe enum: it
was observed holding a plain label (`VRBO`, `Manual`), an opaque numeric pair, and
an embedded JSON blob containing channel listing identifiers and a confirmation
code. **Prefer `source`; treat `source_text` as untrusted display text and do not
parse it.**

Booking objects also carry guest contact details, financial fields, IP address and
notes. **None of that is recorded here, and none of it may be forwarded.**

*Evidence: VERIFIED LIVE, 2026-09-02/03. Structural findings only — no guest names,
emails, phone numbers, booking identifiers or reservation payloads are recorded in
this document.*

### The resolution bridge

Booking data is the supported bridge from a safe reference to a provider thread:

```
safe AgentGuard conversation/booking reference
   → provider booking (resolved server-side)
      → thread_uid (resolved server-side)
```

**The model must never supply `booking_id` or `thread_uid` directly.** Both are
provider identifiers; a model that can name one can address an arbitrary
reservation. This is the same boundary as `property_slug` in §3.

*Evidence: INFERENCE / FUTURE WORK (AgentGuard design decision, following the
existing connector invariant).*

---

## 7. Messaging read API

**`GET /v2/messaging/{threadGuid}`** — "Retrieve a message thread with the
specified Id."

Classification: **supported, documented Lodgify Public API.**

### Published fields

Lodgify's published schema documents:

```
guid, subject, is_read, is_archived,
messages[].id, messages[].type, messages[].subject,
messages[].message, messages[].created_at
```

### Richer fields observed live

The live response is **materially richer than the published schema**. Model
against the live shape, not the published spec.

Thread object:

| Field | Notes |
|---|---|
| `thread_uid` | The live spelling. Published schema says `guid`. |
| `subject` | |
| `is_read` | |
| `is_closed` | Live spelling. Published schema says `is_archived`. |
| `last_message_date` | Not in the published schema. |
| `guest_name` | **PII — never forward.** |
| `guest_email` | **PII — never forward.** |
| `error_title`, `error_message` | Not in the published schema. |
| `messages[]` | |

Message object:

| Field | Notes |
|---|---|
| `id` | Integer row id. |
| `message_id` | UUID. Not in the published schema. |
| `type` | Sender type. |
| `subject` | |
| `message` | Body text. |
| `date_created` | Live spelling. Published schema says `created_at`. |
| `is_read` | |
| `is_imported` | Not in the published schema. |
| `message_status` | Not in the published schema. **The delivery signal — see §13.** |
| `route` | Not in the published schema. **Not a delivery predicate — see §12.** |
| `attachments` | Array. |

### Observed enum values

**Sender types (`type`):**

```
Owner     — outbound, from the property operator
Renter    — inbound, from the guest
```

**Routes (`route`):**

```
Airbnb
BookingCom
Vrbo
Sms
Email
null      (no route recorded)
```

**Delivery statuses (`message_status`):**

```
Delivered
Sent
Failed
Unknown
```

### Ordering — messages are newest-first

**`messages[]` was observed returned newest-first**, not chronologically. This is
easy to get wrong: an initial scan during investigation read `messages[-1]` as
"the latest message" and reported the *oldest* message for every thread before the
error was caught.

**AgentGuard normalizes to chronological (oldest-first) order** for reasoning and
UI. Rationale: a model reasons about a conversation the way a person reads one, and
a UI that renders newest-first would invert every thread. The normalization is done
once, explicitly, at the sanitization boundary — never left to the caller.

*Evidence: VERIFIED LIVE, 2026-09-02/03, across a sample of recent threads spanning
all six `source` values.*

---

## 8. Inbox list limitation

**There is no documented public "list message threads" endpoint currently proven.**

Lodgify's published documentation index contains exactly one messaging read
endpoint — `GET /v2/messaging/{threadGuid}` — which requires a thread GUID the
caller must already possess. There is no `GET /v2/messaging` and no thread search.

The Lodgify dashboard's own conversation list appears to be served by a **private
application API** on `app.lodgify.com`, authenticated by the operator's browser
session. A Lodgify community discussion ("Access Reservation Thread") is the known
reference point: a `threads` endpoint exists in the dashboard but was not exposed
publicly; `GET /v2/messaging/{threadGuid}` was later published as the partial
answer.

**AgentGuard must not depend on that private browser API.** See §22.

### Supported alternatives

1. **Reservation/booking list** (`GET /v2/reservations/bookings`) — the approach
   AgentGuard uses. Every booking carries a `thread_uid` (§6), so the booking list
   *is* the conversation index.
2. **Message-received webhook**, if the event name can be confirmed (§9).

*Evidence: DOCUMENTED BY LODGIFY (absence of a list endpoint in the published
index, verified by reading Lodgify's own `llms.txt` documentation index);
LIKELY / NOT YET VERIFIED (that the dashboard list is a private API — inferred from
the community discussion and from its absence in the public surface, not confirmed
by inspecting the dashboard).*

---

## 9. Webhook findings

### Supported management surface

```
GET    /webhooks/v1/list
POST   /webhooks/v1/subscribe      body: {event, target_url}
DELETE /webhooks/v1/unsubscribe    body: {id}
```

*Evidence: DOCUMENTED BY LODGIFY. Not exercised — subscribing is a write and was
out of scope.*

Known event names, from the Priyanka Homes investigation: `booking_new_any_status`,
`booking_change`. *Evidence: DOCUMENTED BY LODGIFY (topic names only).*

### A guest-message event — LIKELY / NOT YET VERIFIED

Lodgify's Zapier integration exposes a trigger described as *"triggers when a new
guest message is received in a thread"*. That is strong evidence such an event
exists and is reachable through Lodgify's own webhook infrastructure.

**Its exact event name has NOT been verified against Lodgify's authoritative event
enum.** Lodgify's webhook reference page is behind a Cloudflare challenge and could
not be read directly during investigation, and the event does not appear in the
machine-readable documentation index.

**Do not invent the event name.** Before building against it, read the name from
Lodgify's own webhook reference or confirm it with Lodgify Support.

### Signature verification — UNRESOLVED

Third-party integration guides describe an `x-lodgify-signature` HMAC-SHA256
header. **This was not confirmed from Lodgify's official documentation**, and the
same third-party source stated a subscribe path (`POST /v2/webhooks`) that
contradicts Lodgify's published spec (`POST /webhooks/v1/subscribe`) — so that
source is not reliable.

**Signature verification details are explicitly unresolved.** Do not implement
webhook signature checking from the third-party description.

*Evidence: LIKELY / NOT YET VERIFIED (event exists); UNRESOLVED (event name,
signature scheme).*

---

## 10. Send message API

**`POST /v1/reservation/booking/{id}/messages`**

Lodgify's documented description: *"Add one or more messages for a specific
booking."* A sibling endpoint exists for enquiries:
`POST /v1/reservation/enquiry/{id}/messages`.

| | |
|---|---|
| Authentication | `X-ApiKey` |
| Content-Type | `application/*+json` |
| Request body | **A JSON array** of message objects |

### Request body

```json
[
  {
    "subject": "Thank you",
    "message": "Thank you for your question.",
    "type": "Owner",
    "send_notification": true
  }
]
```

This is the exact body used in the controlled live send (§11).

**The controlled target was the account owner's own test reservation** — a
`PublicApi`-sourced booking created during an earlier authorized write test, whose
guest contact address is the Lodgify account owner's own address. No real customer
was messaged. **The recipient address is not recorded in this document.**

*Evidence: DOCUMENTED BY LODGIFY (endpoint, description, body shape);
DOCUMENTED + VERIFIED LIVE (the exact body above — see §11).*

---

## 11. Controlled live send results

> ### VERIFIED LIVE — 2026-09-03
>
> **Exactly one POST was issued.** No second send has been made.

### Response

```
HTTP/1.1 200
Content-Type: (absent)
Content-Length: 0
body: (empty)
```

### New message observed on the thread

```
type            = Owner
subject         = "Thank you"
message         = "Thank you for your question."
message_status  = Delivered
route           = null
is_read         = true
is_imported     = false
attachments     = []
```

Subject and body were stored **verbatim** — no rewriting, wrapping, templating or
HTML transformation. The `type` submitted was preserved exactly.

The provider message identifier and thread GUID are deliberately **omitted**: they
are provider identifiers with no engineering value in a reference document.

### Independent confirmation of external delivery

Delivery was confirmed **outside Lodgify**, by observing the account owner's own
mailbox:

- an email arrived **approximately 2 seconds after** the message row's creation
  timestamp;
- **subject and body matched exactly**;
- the sender address **mapped back to the Lodgify thread** — its local part encodes
  the thread identifier, so a guest reply returns to the same thread.

**The recipient address is not recorded here.**

### Conclusion

**The endpoint performs a real, externally-visible send. It is not merely an
internal note append.**

*Evidence: VERIFIED LIVE, 2026-09-03, one controlled run, one thread, one booking.
Not repeated, not exercised across channels.*

---

## 12. Critical route finding

> ### `route` is NOT a delivery predicate.

The controlled send produced:

```
route          = null
message_status = Delivered
```

**and external email delivery was independently confirmed.**

Therefore code must **never** interpret:

```
route == null   ⟹   not delivered
```

That inference is empirically false. A message with no route recorded was really
delivered, to a real mailbox, within two seconds.

Delivery interpretation must use `message_status`, with appropriate uncertainty
(§13). If `route` is surfaced at all, it must be labelled **informational only**.

*Evidence: VERIFIED LIVE, 2026-09-03. This is the single most important finding in
this document — it is the one that a reasonable implementer would otherwise get
wrong.*

---

## 13. Message status

Observed values and AgentGuard's conservative interpretation:

| Value | Provider meaning | AgentGuard interpretation |
|---|---|---|
| `Delivered` | Provider reports delivered | Report as "Lodgify reports the message as Delivered." Do not claim which channel. |
| `Sent` | Provider reports sent | Sent; **delivery not confirmed**. Do not upgrade to "delivered". |
| `Failed` | Provider reports failure | Treat as a real failure. |
| `Unknown` | No delivery state | Unknown. Never render as success or failure. |

`Failed` is not hypothetical: it was **observed live** on another booking during
read-only investigation, on an email-routed thread. *(That booking's details are
not recorded here.)*

**Do not make stronger channel-delivery claims than the data supports.** See §19.

*Evidence: VERIFIED LIVE (all four values observed across the sampled threads).*

---

## 14. Fan-out

### Observed dashboard behaviour

**One logical owner send can produce multiple message rows — one per route.**

On one live thread, a single owner message appeared as **two rows sharing the same
subject and the same timestamp**, differing only in route and status:

```
subject "Parking Info"  route=Sms    message_status=Sent
subject "Parking Info"  route=Vrbo   message_status=Delivered
```

The same pattern was visible in the mailbox as two separate emails for one logical
message. *(No guest information from those threads is recorded here.)*

### The controlled API send did not fan out

```
one POST  →  one message row  →  one email
```

The test thread was `PublicApi`-sourced with a single email route, so there was
nothing to fan out to.

### Conclusion

**AgentGuard must not assume one POST == one message row.**

Post-send verification must accept **multiple** newly-created matching rows and
report all of them. Fan-out appears to be a property of the *thread's configured
routes*, not of the endpoint — which means a caller cannot predict the row count in
advance, and must not treat "more than one row" as an anomaly.

*Evidence: VERIFIED LIVE (dashboard-originated fan-out observed; API-originated
single row observed). INFERENCE / FUTURE WORK: that API-originated sends on a
multi-route thread would fan out the same way — plausible but unverified.*

---

## 15. No reservation mutation

Before and after the controlled send, **22 whitelisted booking fields were
compared**. The diff was empty.

Fields checked included:

```
status              arrival             departure           property_id
source              is_deleted          canceled_at         total_amount
amount_paid         amount_due          currency_code       tentative_expires_at
updated_at          created_at          is_new              is_overbooked
is_unavailable      check_in            check_out           language
transaction count   room count
```

Notably **`updated_at` did not move** — the booking record was not touched at all.

The thread's `last_message_date` advanced as expected, and the thread's `is_read`
state was unchanged.

**This is evidence that the messaging operation is bounded to the conversation
rather than mutating reservation state.**

*Evidence: VERIFIED LIVE, 2026-09-03, one booking. Not an exhaustive audit of every
field the API can return, and not repeated across booking statuses.*

---

## 16. Empty response / the verification problem

> **The POST returns no created message identifier.** The response body is empty.

Therefore AgentGuard **cannot correlate the created message directly from the
response**. There is no id to record, no handle to follow, and nothing to put in an
audit trail from the call itself.

### Required design

```
1.  pre-send thread snapshot        — record existing message identifiers
2.  exactly one POST                — never retried (§17)
3.  post-send thread read
4.  diff message identifiers        — new rows are those not in the snapshot
5.  cautiously match new Owner rows using:
        type == "Owner"
        exact subject
        exact message text
        created-after timestamp
```

**Do not assume one row** — fan-out exists (§14). Return every matching row.

### Ambiguity

Snapshot-then-diff is inherently racy: another actor (the dashboard, an automation)
can write to the same thread inside the window.

- A concurrent **unrelated** message is excluded by exact subject + body matching.
- A concurrent **identical** message cannot be told apart from ours.

**If correlation is ambiguous, the outcome is `UNKNOWN_SEND_STATE`** — never a
guess about which row was ours. Nothing is ever overwritten or deleted to resolve
ambiguity.

*Evidence: VERIFIED LIVE (empty response); INFERENCE / FUTURE WORK (the
snapshot/diff algorithm is AgentGuard's design response to it).*

---

## 17. No idempotency / no retry

> ### PERMANENT SAFETY INVARIANT

**No Lodgify idempotency mechanism has been identified for this send endpoint.**
No idempotency key parameter, no header, no client-supplied message id, no
deduplication guarantee. This mirrors the same gap the Priyanka Homes project
recorded for reservation creation.

**Therefore `send_guest_reply` MUST NEVER automatically retry.**

Do not place generic retry middleware around this POST. Do not add a "transient
error" retry. Do not retry on timeout.

### Why

A timeout after the request has left the process is **ambiguous**:

- the message may have been sent successfully and the response lost; or
- the message may never have been sent.

**These are indistinguishable from the client.** Retrying could send a second real,
externally-visible message to a real guest, with no way to detect or recall it.

### The three outcomes

| Outcome | Meaning |
|---|---|
| `CONFIRMED_SENT` | The POST succeeded **and** the post-send re-read found a matching outbound message. |
| `CONFIRMED_FAILED` | The provider returned an explicit failure **before** any ambiguous send state arose. Safe to treat as "nothing was sent." |
| `UNKNOWN_SEND_STATE` | A timeout or network failure occurred after the request may have left the process, **or** the POST succeeded but verification could not establish the resulting row, **or** correlation was ambiguous. |

**`UNKNOWN_SEND_STATE` must explicitly mean: DO NOT AUTOMATICALLY RESEND.**

It requires human review. The result returned to the model must say so in words,
not merely by status code — the model must be told not to retry, because a model
that sees a non-success outcome will otherwise try again by default.

*Evidence: VERIFIED LIVE (no idempotency field in the documented request shape or
the observed behaviour); INFERENCE / FUTURE WORK (the three-outcome model is
AgentGuard's design).*

---

## 18. Caller-controlled `type`

The API **accepted `type: "Owner"` exactly as submitted** and stored it verbatim.
The field appears to be **caller-controlled**, not derived by the provider from the
credential.

The implication is uncomfortable: an API caller may well be able to submit
`type: "Renter"` and **forge an inbound message** that reads as if the guest wrote
it. *(This was not tested — doing so would create a real forged message in a real
thread. It is recorded as a risk, not as a verified capability.)*

**AgentGuard must NOT expose `type` as a model argument.** Pin it server-side:

```
type = "Owner"
```

Likewise **`send_notification` must not be model-controlled.** Pin the intended
value server-side for the chosen action. A model that can set
`send_notification: false` can write into a guest thread without notifying anyone —
a silent write is exactly the kind of action governance exists to prevent.

*Evidence: VERIFIED LIVE (`type` accepted and preserved verbatim);
INFERENCE / FUTURE WORK (`Renter` forgery is a reasoned risk, deliberately not
tested).*

---

## 19. OTA routing limitation

> ### OBSERVED BUT NOT YET VERIFIED ACROSS OTA CHANNELS

The controlled live send used a **`PublicApi` test booking whose thread delivered
by email**. That is the only channel an API-originated send has been observed on.

**API-originated sending to Airbnb, Booking.com and Vrbo has NOT been verified
live.**

Dashboard-originated messages on OTA threads *have* shown `route` values of
`Vrbo`, `Airbnb` and `BookingCom` with `message_status = Delivered` — but **that
does not prove the API POST follows the same routing semantics.** A dashboard send
and an API send are different code paths on Lodgify's side, and the one thing we
know for certain about `route` is that it did not behave as expected on the API
path (§12).

### Wording policy

**Do not claim:**

```
"sent to Airbnb"
"sent to Vrbo"
"sent to Booking.com"
```

unless provider data actually establishes it.

**Prefer:**

- `"Lodgify accepted the message"` — after a successful POST;
- `"Lodgify reports the message as Delivered."` — after a re-read returns
  `message_status = Delivered`.

**Do not translate `route == null` into failure** (§12).

### How this gets resolved

Wait until an actual guest message on an OTA thread **deserves a reply**, then send
that reply through AgentGuard itself. That becomes the real production
verification — a genuinely warranted message, sent under approval, observed. Do not
manufacture a test send to a real guest.

*Evidence: VERIFIED LIVE (email route); OBSERVED BUT NOT YET VERIFIED ACROSS OTA
CHANNELS (everything else).*

---

## 20. AgentGuard risk decision

```
send_guest_reply  →  ToolRisk.DANGEROUS
```

**Mandatory human approval, every time, in V1.** No auto-approval, no allowlist, no
"trusted intent" bypass.

### Reasoning

- **Externally visible** — it reaches a real third party outside the operator's
  organisation.
- **Irreversible** — no edit or delete endpoint has been proven to exist. Once sent,
  it cannot be recalled.
- **No idempotency** (§17) — a retry sends a second real message.
- **Ambiguous timeout creates duplicate-message risk** — the client cannot always
  know whether it already sent.
- **Reputational impact** — a wrong message to a guest damages the business in a way
  a wrong internal record does not.

`WRITE` would be defensible only if the action were confined to internal notes.
It is not.

### Governance note — a product-policy issue, not a code change

Under the current model, **`APPROVE_DANGEROUS` is ADMIN-only**. That means every
routine guest reply requires an ADMIN approver, which may be too restrictive for
day-to-day Priyanka Homes operation.

**This milestone does not change the global DANGEROUS permission model.** Weakening
`APPROVE_DANGEROUS` to make guest messaging convenient would also weaken approval
for every other dangerous action in the system — that trade is not messaging's to
make.

The forward options, to be decided explicitly rather than by drift:

1. a dedicated hospitality permission, e.g. `APPROVE_GUEST_MESSAGE`, granted to a
   role that is not ADMIN; or
2. an explicit policy reclassification of guest messaging, argued on its own merits.

*Evidence: INFERENCE / FUTURE WORK (AgentGuard policy decision).*

---

## 21. Privacy and sanitization

### What upstream objects contain

Lodgify responses carry, variously:

```
guest name          guest email         guest phone
booking identifiers thread identifiers  channel/source data
IP address          financial fields    internal notes
raw nested payloads
```

### The rule

**AgentGuard constructs safe results field-by-field from named upstream fields.**
No passthrough, no `**rest`, no `dict(response)`. An upstream payload that grows a
new field cannot reach anything downstream by default — it has to be added
deliberately.

**Never forward raw provider JSON into:**

```
model context      RunStep      audit      observability      browser
```

### Two deliberate exceptions

Both are recorded here because they are real privacy trade-offs, consciously made:

**1. Message bodies enter model context.**

Guest message text is intentionally allowed into model context, because **drafting a
reply requires reading the question**. There is no version of this feature where the
model reasons about a conversation it cannot see.

What this means: guest-authored prose — which may itself contain a name, a phone
number, or travel details the guest chose to share — reaches the model and is
persisted in the run's conversation. The structured PII fields (`guest_email`,
`guest_phone`, `guest_name`) are still dropped at construction; only the message
body survives, and only because it is the subject of the task.

**2. Outbound approved message text is stored in the audit log.**

The approved message is intentionally persisted. It is **the externally-visible
business action being authorized** — an audit trail that records "a message was
approved" without recording *which message* would be useless for the one question a
reviewer will actually ask.

This is the same reasoning that puts tool arguments in the audit log generally: the
audit exists to reconstruct what was done, and for this tool the message text *is*
what was done.

**Not audited:** the API key, guest email, guest phone, raw booking JSON, raw thread
JSON.

*Evidence: INFERENCE / FUTURE WORK (AgentGuard policy). The existing connector's
sanitization is enforced by tests; the messaging equivalents will need the same.*

---

## 22. Supported vs. private API policy

> ### DURABLE POLICY

**The AgentGuard Lodgify integration uses supported `api.lodgify.com` APIs only.**

Do not use:

```
private app.lodgify.com endpoints
browser cookies
human session tokens
scraped dashboard APIs
```

unless a future **explicit architecture and security decision** reverses this — and
records the reversal here.

### Why

A private dashboard API is authenticated by a *human's browser session*. Depending
on it would mean AgentGuard holding and replaying an operator's session cookie,
which breaks the identity invariant (identity comes from a token, not a smuggled
browser credential) and the credential invariant (one resolver, never stored). It
also has no documented contract, no versioning, no deprecation notice and no rate
limits — an audit trail over an endpoint that can silently change shape is not a
durable audit trail.

### Browser inspection is still allowed — for discovery only

Inspecting the dashboard's network traffic to *understand* what exists is
legitimate research. **It is never the production authentication model.** The
distinction is: discovery may look at anything; the shipped integration talks only
to documented endpoints with `X-ApiKey`.

*Evidence: INFERENCE / FUTURE WORK (AgentGuard policy).*

---

## 23. Current AgentGuard Lodgify V1

Implemented today — **all READ, no write method exists in the codebase**:

| Tool | Risk | Provider call |
|---|---|---|
| `list_properties` | READ | none — configuration only |
| `get_property_availability` | READ | `GET /v2/availability/{id}` |
| `get_property_quote` | READ | `GET /v2/quote/{id}` |

`app/connectors/lodgify/client.py` exposes exactly two GET methods. There is no
POST, PUT, PATCH or DELETE anywhere in the connector.

**Messaging is the next connector increment** and should be built from the findings
in this document — in particular §12 (route is not delivery), §14 (fan-out), §16
(empty response / snapshot-diff), §17 (no retry), §18 (pin `type` and
`send_notification`), and §20 (DANGEROUS + mandatory approval).

---

## 24. Evidence and decision log

| Finding | Evidence level | Date | Implication |
|---|---|---|---|
| `X-ApiKey` header auth, `api.lodgify.com` | DOCUMENTED + VERIFIED LIVE | 2026-08-19 / 09-02 | Single server-side credential; one resolver, no fallback. |
| `GET /v2/availability/{id}`; `start`/`end`, not `from`/`to` | VERIFIED LIVE | 2026-08-19 | Wrong params return plausible placeholder data — a wrong answer that looks right. |
| `GET /v2/quote/{id}`; `guestbreakdown` exact spelling | DOCUMENTED + VERIFIED LIVE | 2026-08-19 | Underscored variant returns 500, not a clean 400. |
| Bookings carry `thread_uid`; 143/143 had one | VERIFIED LIVE | 2026-09-02 | The booking list is the usable conversation index. |
| `source` enum: Airbnb / BookingCom / HomeAway / Manual / OH / PublicApi | VERIFIED LIVE | 2026-09-02 | Safe channel signal. `source_text` is untrusted free text. |
| `GET /v2/messaging/{threadGuid}` | DOCUMENTED + VERIFIED LIVE | 2026-09-02 | The only supported thread read. |
| Live thread schema richer than published spec | VERIFIED LIVE | 2026-09-02 | Model against live shape; `message_status` and `route` are undocumented. |
| `messages[]` returned newest-first | VERIFIED LIVE | 2026-09-02 | Must be normalized; misreading this inverted an entire investigation scan. |
| No public list-threads endpoint | DOCUMENTED BY LODGIFY | 2026-09-02 | Resolve threads via bookings; never via the private dashboard API. |
| `POST /v1/reservation/booking/{id}/messages` | DOCUMENTED + VERIFIED LIVE | 2026-09-03 | JSON **array** body; `application/*+json`. |
| POST returns HTTP 200 with **empty body** | VERIFIED LIVE | 2026-09-03 | No created id ⇒ snapshot/diff verification is mandatory. |
| External email delivery independently confirmed (~2s) | VERIFIED LIVE | 2026-09-03 | A real send, not an internal note append. |
| **`route = null` despite confirmed delivery** | VERIFIED LIVE | 2026-09-03 | **`route` is not a delivery predicate.** Use `message_status`. |
| Subject and body stored verbatim | VERIFIED LIVE | 2026-09-03 | Approved text == sent text is achievable and must be enforced. |
| No reservation mutation (22 fields, `updated_at` unmoved) | VERIFIED LIVE | 2026-09-03 | Action is bounded to the conversation. |
| One logical send can fan out to multiple rows | VERIFIED LIVE (dashboard) | 2026-09-02 | Verification must accept N rows, not assume 1. |
| `type` accepted verbatim; appears caller-controlled | VERIFIED LIVE | 2026-09-03 | Pin `type="Owner"` server-side; `Renter` forgery is a plausible risk. |
| No idempotency key | VERIFIED LIVE / DOCUMENTED absence | 2026-09-03 | **Never auto-retry.** `UNKNOWN_SEND_STATE` requires human review. |
| `message_status` values: Delivered / Sent / Failed / Unknown | VERIFIED LIVE | 2026-09-02/03 | `Failed` is real and observed. |
| API-originated OTA routing | OBSERVED BUT NOT YET VERIFIED ACROSS OTA CHANNELS | 2026-09-03 | Do not claim Airbnb/Vrbo/Booking.com delivery. |
| Guest-message webhook event | LIKELY / NOT YET VERIFIED | 2026-09-02 | Event exists per Zapier surface; **name unconfirmed — do not invent it.** |
| Webhook signature scheme | UNRESOLVED | 2026-09-02 | Third-party description contradicts the published spec; do not implement from it. |
| `send_guest_reply` = `ToolRisk.DANGEROUS` | INFERENCE / FUTURE WORK | 2026-09-03 | Mandatory approval; ADMIN-only today — a product-policy issue, not a code change. |

---

## Appendix: sources

- Lodgify published documentation index (`docs.lodgify.com/llms.txt`) — the
  machine-readable list of every documented endpoint. The HTML and `.md` reference
  pages are behind a Cloudflare challenge and could not be read directly.
- Lodgify published OpenAPI (`Lodgify Public API`, v2.0).
- Lodgify community discussion "Access Reservation Thread" (thread-list gap).
- Live account inspection, read-only, 2026-09-02 — 143 bookings, sampled threads.
- One controlled live send, 2026-09-03, against the account owner's own test
  reservation, with prior explicit operator authorization.
- Priyanka Homes `docs/LODGIFY_API.md` — prior verified knowledge (availability,
  rates, quote, property mapping, the 2026-08-19 controlled write test).
