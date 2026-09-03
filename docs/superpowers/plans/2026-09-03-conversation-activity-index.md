# Conversation Activity Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist conversation activity metadata so a webhook-identified Historic conversation appears in the Inbox without scanning every historic thread.

**Architecture:** A new metadata-only `conversation_activity` table, upserted by both the webhook path and the `Current + Upcoming` poll. A new `app/inbox_view.py` merges live summaries with persisted rows, orders by `last_message_at`, applies the limit, then live-enriches only the persisted-only rows that survived — using one shared `all_bookings(stayFilter="All")` scan per request, never one per row.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x, Alembic, pytest, httpx MockTransport; React 19 + TypeScript + Vitest.

**Spec:** `docs/superpowers/specs/2026-09-03-conversation-activity-index-design.md`

## Global Constraints

- **Preserve the proactive-drafting WIP untouched.** `app/drafts.py`, `app/conversation_refresh.py`, `app/cancellation.py`, `app/early_check_in.py`, `app/late_checkout.py`, `app/owner_knowledge.py`, `alembic/versions/6c03159077a5_add_conversation_drafts.py` and the drafting tests are **added to, never reverted, reformatted, or rewritten**. Additive edits only.
- **No guest text or PII anywhere** — not in the table, not in fixtures, not in this plan, not in docs. Fixture text is invented.
- **No provider identifiers persisted.** No `booking_id`, no `thread_uid` column, ever.
- **No contact/phone work in this milestone.** Explicitly out of scope.
- **`ResolvedConversation` never leaves the connector.** It carries `booking_id` and `thread_uid`.
- **Enrichment pages the booking archive once per request**, regardless of how many persisted-only rows are on the page.
- **Ordering signal is the persisted `last_message_at`.** Never read a thread in order to decide sort order.
- **Do not alter existing migrations.** New revision only, single head.
- **Tests never touch `agentops.db`.** Always take the `database` fixture and pass `database=database`.
- **Nothing here sends.** No new write path to the provider.

---

### Task 1: `conversation_activity` table and migration

**Files:**
- Modify: `app/db_models.py` (append a new model beside `ConversationDraftRecord`)
- Create: `alembic/versions/<generated>_add_conversation_activity.py`
- Modify: `tests/test_migrations.py` (extend existing coverage)

**Interfaces:**
- Consumes: `Base` from `app.database`; the existing head revision `6c03159077a5`.
- Produces: `ConversationActivityRecord` with columns `id`, `conversation_ref`, `property_slug`, `source`, `booking_status`, `last_message_at`, `last_message_sender`, `message_count`, `conversation_fingerprint`, `status`, `first_seen_at`, `last_refreshed_at`.

- [ ] **Step 1: Write the failing migration tests**

Append to `tests/test_migrations.py`:

```python
def test_conversation_activity_table_exists(migrated):
    assert "conversation_activity" in inspect(migrated).get_table_names()


def test_conversation_activity_stores_no_guest_text_or_provider_ids(migrated):
    """The absence of these columns is the safety property, not a convention."""
    columns = {
        column["name"]
        for column in inspect(migrated).get_columns("conversation_activity")
    }

    forbidden = {
        "booking_id",
        "thread_uid",
        "guest_name",
        "guest_email",
        "guest_phone",
        "phone",
        "email",
        "last_message_excerpt",
        "excerpt",
        "message",
        "message_body",
        "payload",
    }

    assert columns & forbidden == set()


def test_conversation_activity_has_exactly_the_agreed_columns(migrated):
    columns = {
        column["name"]
        for column in inspect(migrated).get_columns("conversation_activity")
    }

    assert columns == {
        "id",
        "conversation_ref",
        "property_slug",
        "source",
        "booking_status",
        "last_message_at",
        "last_message_sender",
        "message_count",
        "conversation_fingerprint",
        "status",
        "first_seen_at",
        "last_refreshed_at",
    }


def test_conversation_ref_is_unique_in_the_activity_index(migrated):
    """One conversation is one row; upsert depends on this."""
    indexes = inspect(migrated).get_indexes("conversation_activity")

    assert any(
        index["column_names"] == ["conversation_ref"] and index["unique"]
        for index in indexes
    )


def test_migrations_seed_no_activity_rows(migrated):
    with migrated.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM conversation_activity")
        ).scalar()

    assert count == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_migrations.py -q -k conversation_activity`
Expected: FAIL — `conversation_activity` is not a table.

- [ ] **Step 3: Add the model**

Append to `app/db_models.py`:

```python
class ConversationActivityRecord(Base):
    """The latest known activity of one conversation.

    An index, not an archive. It exists so a conversation the Inbox does not
    enumerate -- a Historic stay, reachable only because a webhook named it --
    can still be listed and ordered by recency.

    Metadata only. There is deliberately no column that could hold a message
    body, an excerpt, a guest's name, email or phone, a booking id or a thread
    uid. Guest text lives in Lodgify, transiently in model context, and in
    sanitized historical reply storage under its own rules. Never here.

    `property_name` is not stored either: it is derived from `property_slug`
    through configuration, so a rename cannot leave stale display text behind.
    `needs_attention` is not stored: it is derived from `status`, so the two
    cannot disagree.
    """

    __tablename__ = "conversation_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # One conversation is one row. The upsert depends on this being unique.
    conversation_ref: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    property_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)

    source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    booking_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The ordering signal for the whole Inbox.
    last_message_at: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )

    last_message_sender: Mapped[str | None] = mapped_column(String(32), nullable=True)

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    conversation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # A ConversationStatus value.
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    first_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)

    last_refreshed_at: Mapped[str] = mapped_column(String(32), nullable=False)
```

Confirm `Integer`, `String`, `Mapped`, `mapped_column` are already imported in that module; add only what is missing.

`alembic/env.py` already does `from app import db_models  # noqa: F401`, so the new model is discovered. **Verify this and change nothing** — a second import line is not needed.

- [ ] **Step 4: Generate and read the migration**

```bash
uv run alembic revision --autogenerate -m "add conversation activity"
```

Open the generated file and read it before running it. Confirm: `down_revision` is `"6c03159077a5"`; it creates only `conversation_activity`; it drops nothing; it seeds nothing. Autogenerate is a first draft — fix it if it proposes anything else.

- [ ] **Step 5: Run the migration tests**

Run: `uv run pytest tests/test_migrations.py -q`
Expected: PASS, including the pre-existing `test_there_is_a_single_head`, `test_a_fresh_database_upgrades_from_zero_to_head`, `test_upgrading_a_populated_baseline_database_preserves_data`, `test_migrations_seed_no_data` and `test_the_development_database_is_untouched`. These already cover "existing DB → head", "fresh DB → head", "single head" and "no seeded rows"; do not duplicate them.

- [ ] **Step 6: Commit**

```bash
git add app/db_models.py alembic/versions tests/test_migrations.py
git commit -m "feat: add the conversation activity index table"
```

---

### Task 2: `ConversationActivityStore`

**Files:**
- Create: `app/conversation_activity.py`
- Create: `tests/test_conversation_activity.py`

**Interfaces:**
- Consumes: `ConversationActivityRecord` (Task 1); `Database`/`get_database` from `app.database`.
- Produces:
  - `@dataclass(frozen=True) ConversationActivity` with the eleven fields plus `to_row() -> dict[str, Any]` and `needs_attention -> bool` (derived from `status`).
  - `ConversationActivityStore(database: Database | None = None)` with:
    - `upsert(conversation_ref: str, conversation_fingerprint: str, status: str, last_message_at: str | None, last_message_sender: str | None, message_count: int, property_slug: str | None = None, source: str | None = None, booking_status: str | None = None) -> ConversationActivity`
    - `all_activity() -> list[ConversationActivity]`
    - `for_conversation(conversation_ref: str) -> ConversationActivity | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_conversation_activity.py`:

```python
"""The conversation activity index: a metadata snapshot, never an archive."""

from app.conversation_activity import ConversationActivityStore
from app.connectors.lodgify.messaging_models import ConversationStatus


def activity(store, ref="PH-AAAA1111", at="2026-09-03T12:06:33", **kwargs):
    return store.upsert(
        conversation_ref=ref,
        conversation_fingerprint=kwargs.pop("fingerprint", "fp-1"),
        status=kwargs.pop("status", ConversationStatus.NEEDS_ATTENTION.value),
        last_message_at=at,
        last_message_sender=kwargs.pop("sender", "Renter"),
        message_count=kwargs.pop("message_count", 3),
        property_slug=kwargs.pop("property_slug", "renovated-2nd-floor-home"),
        source=kwargs.pop("source", "BookingCom"),
        booking_status=kwargs.pop("booking_status", "Booked"),
    )


def test_an_upsert_stores_one_row(database):
    store = ConversationActivityStore(database=database)

    activity(store)

    rows = store.all_activity()

    assert len(rows) == 1
    assert rows[0].conversation_ref == "PH-AAAA1111"
    assert rows[0].last_message_at == "2026-09-03T12:06:33"


def test_a_repeated_upsert_updates_the_same_row(database):
    """A re-delivered webhook must not create a second conversation."""
    store = ConversationActivityStore(database=database)

    first = activity(store)
    activity(store, at="2026-09-03T14:00:00", fingerprint="fp-2")

    rows = store.all_activity()

    assert len(rows) == 1
    assert rows[0].last_message_at == "2026-09-03T14:00:00"
    assert rows[0].conversation_fingerprint == "fp-2"
    # first_seen_at is when we first learned of it, and never moves.
    assert rows[0].first_seen_at == first.first_seen_at
    assert rows[0].last_refreshed_at >= first.last_refreshed_at


def test_needs_attention_is_derived_not_stored(database):
    store = ConversationActivityStore(database=database)

    activity(store, status=ConversationStatus.NEEDS_ATTENTION.value)
    assert store.for_conversation("PH-AAAA1111").needs_attention is True

    activity(store, status=ConversationStatus.RESPONDED.value)
    assert store.for_conversation("PH-AAAA1111").needs_attention is False


def test_the_row_projection_carries_no_guest_text_or_provider_ids(database):
    store = ConversationActivityStore(database=database)

    row = activity(store).to_row()

    for forbidden in (
        "booking_id",
        "thread_uid",
        "guest_name",
        "guest_email",
        "guest_phone",
        "last_message_excerpt",
        "message",
    ):
        assert forbidden not in row


def test_an_unknown_conversation_reads_as_none(database):
    store = ConversationActivityStore(database=database)

    assert store.for_conversation("PH-NOTHERE1") is None


def test_the_store_never_touches_the_development_database(
    database, development_database_path
):
    """Isolation proof: the store writes only to the injected database."""
    before = (
        development_database_path.stat().st_mtime_ns,
        development_database_path.stat().st_size,
    )

    activity(ConversationActivityStore(database=database))

    after = (
        development_database_path.stat().st_mtime_ns,
        development_database_path.stat().st_size,
    )

    assert before == after
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_conversation_activity.py -q`
Expected: FAIL — `No module named 'app.conversation_activity'`.

- [ ] **Step 3: Implement the store**

Create `app/conversation_activity.py`:

```python
"""What AgentGuard knows about when each conversation last moved.

An index, not an archive. The Inbox enumerates conversations by paging the
booking list, and that scan deliberately covers only current and upcoming
stays -- reading a thread for all 1062 bookings in the account earns HTTP 429.
So a Historic conversation is never enumerated, however recent its message.

A verified webhook names such a conversation. This is where that knowledge is
kept, so the Inbox can list and order it without crawling the archive.

Metadata only. Nothing here can hold a message, an excerpt, a guest's name,
email or phone, a booking id or a thread uid -- and the schema has no column
that could. `needs_attention` is derived from `status` rather than stored, so
the two cannot disagree.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.connectors.lodgify.messaging_models import ConversationStatus
from app.database import Database, get_database
from app.db_models import ConversationActivityRecord

__all__ = ["ConversationActivity", "ConversationActivityStore"]


@dataclass(frozen=True)
class ConversationActivity:
    """One conversation's latest known activity."""

    conversation_ref: str
    property_slug: str | None
    source: str | None
    booking_status: str | None
    last_message_at: str | None
    last_message_sender: str | None
    message_count: int
    conversation_fingerprint: str
    status: str
    first_seen_at: str
    last_refreshed_at: str

    @property
    def needs_attention(self) -> bool:
        return self.status == ConversationStatus.NEEDS_ATTENTION.value

    def to_row(self) -> dict[str, Any]:
        """The Inbox-row shape, for a conversation we have not read live.

        `last_message_excerpt` is None because no excerpt is stored: it is
        supplied by enrichment, or not at all.
        """
        return {
            "conversation_ref": self.conversation_ref,
            "fingerprint": self.conversation_fingerprint,
            "property_slug": self.property_slug,
            "property_name": None,
            "source": self.source,
            "booking_status": self.booking_status,
            "status": self.status,
            "last_message_at": self.last_message_at,
            "last_message_sender": self.last_message_sender,
            "last_message_excerpt": None,
            "message_count": self.message_count,
        }


def _read(record: ConversationActivityRecord) -> ConversationActivity:
    return ConversationActivity(
        conversation_ref=record.conversation_ref,
        property_slug=record.property_slug,
        source=record.source,
        booking_status=record.booking_status,
        last_message_at=record.last_message_at,
        last_message_sender=record.last_message_sender,
        message_count=record.message_count,
        conversation_fingerprint=record.conversation_fingerprint,
        status=record.status,
        first_seen_at=record.first_seen_at,
        last_refreshed_at=record.last_refreshed_at,
    )


class ConversationActivityStore:
    """Persistence for conversation activity metadata."""

    def __init__(self, database: Database | None = None) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database or get_database()

    def upsert(
        self,
        conversation_ref: str,
        conversation_fingerprint: str,
        status: str,
        last_message_at: str | None,
        last_message_sender: str | None,
        message_count: int,
        property_slug: str | None = None,
        source: str | None = None,
        booking_status: str | None = None,
    ) -> ConversationActivity:
        """Record the latest known activity for one conversation.

        Keyed on `conversation_ref`, so a re-delivered webhook and a poll that
        saw the same thing converge on one row. `first_seen_at` is written once
        and never moves; `last_refreshed_at` moves every time.
        """
        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            record = session.scalar(
                select(ConversationActivityRecord).where(
                    ConversationActivityRecord.conversation_ref == conversation_ref
                )
            )

            if record is None:
                record = ConversationActivityRecord(
                    conversation_ref=conversation_ref,
                    first_seen_at=now,
                )
                session.add(record)

            record.property_slug = property_slug
            record.source = source
            record.booking_status = booking_status
            record.last_message_at = last_message_at
            record.last_message_sender = last_message_sender
            record.message_count = message_count
            record.conversation_fingerprint = conversation_fingerprint
            record.status = status
            record.last_refreshed_at = now

            session.flush()

            return _read(record)

    def all_activity(self) -> list[ConversationActivity]:
        with self.database.session() as session:
            return [
                _read(record)
                for record in session.scalars(select(ConversationActivityRecord))
            ]

    def for_conversation(self, conversation_ref: str) -> ConversationActivity | None:
        with self.database.session() as session:
            record = session.scalar(
                select(ConversationActivityRecord).where(
                    ConversationActivityRecord.conversation_ref == conversation_ref
                )
            )

            return _read(record) if record is not None else None
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_conversation_activity.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/conversation_activity.py tests/test_conversation_activity.py
git commit -m "feat: add the conversation activity store"
```

---

### Task 3: `LodgifyInbox.summarise_refs` — one archive scan per request

**Files:**
- Modify: `app/connectors/lodgify/inbox.py` (add one method beside `list_conversations`)
- Modify: `tests/test_inbox.py` (append a new section)

**Interfaces:**
- Consumes: `all_bookings(stay_filters=(STAY_FILTER_ALL,))`, `summarise_all`, both existing.
- Produces: `LodgifyInbox.summarise_refs(refs: set[str]) -> dict[str, dict[str, Any]]` — maps `conversation_ref` to a safe summary dict. Refs that resolve to no booking are absent from the result. Raises nothing for a missing ref.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inbox.py`:

```python
# -- 14. enrichment of conversations the Inbox does not enumerate ----------
#
# A Historic conversation is not in the Current+Upcoming scan, so supplying a
# live excerpt for one means resolving it against the whole archive. Resolving
# each independently would re-page the archive per row -- roughly 11 booking
# calls each -- which is how the 429 happened. One scan serves them all.


def test_summarise_refs_returns_safe_summaries(activity_entries=None):
    fake = activity_fake(
        [
            (1001, "thread-a", "2026-09-03T12:06:33", "Renter"),
            (1002, "thread-b", "2026-08-01T09:00:00", "Owner"),
        ]
    )

    found = fake.inbox().summarise_refs({conversation_ref_for(1001)})

    assert set(found) == {conversation_ref_for(1001)}
    assert found[conversation_ref_for(1001)]["last_message_at"] == (
        "2026-09-03T12:06:33"
    )
    # Provider identifiers never cross this seam.
    assert "thread_uid" not in json.dumps(found)
    assert "booking_id" not in json.dumps(found)


def test_summarise_refs_pages_the_archive_once_for_many_refs():
    """The regression guard for the rate limit. Asserts request count, not time."""
    entries = filler(BOOKING_SCAN_SIZE + 40) + [
        (9001, "thread-x", "2026-09-03T10:00:00", "Renter"),
        (9002, "thread-y", "2026-09-03T11:00:00", "Renter"),
        (9003, "thread-z", "2026-09-03T12:00:00", "Renter"),
    ]

    fake = activity_fake(entries)

    refs = {
        conversation_ref_for(9001),
        conversation_ref_for(9002),
        conversation_ref_for(9003),
    }

    found = fake.inbox().summarise_refs(refs)

    assert set(found) == refs
    # Two pages, scanned once in total -- not once per requested ref.
    assert len(fake.booking_reads) == 2
    # One thread read per requested ref, and no more.
    assert len(fake.thread_reads) == 3


def test_summarise_refs_omits_a_ref_that_resolves_to_nothing():
    fake = activity_fake([(1001, "thread-a", "2026-09-03T12:06:33", "Renter")])

    found = fake.inbox().summarise_refs({conversation_ref_for(9999)})

    assert found == {}


def test_summarise_refs_reads_nothing_when_asked_for_nothing():
    fake = activity_fake([(1001, "thread-a", "2026-09-03T12:06:33", "Renter")])

    assert fake.inbox().summarise_refs(set()) == {}
    assert fake.requests == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_inbox.py -q -k summarise_refs`
Expected: FAIL — `LodgifyInbox` has no attribute `summarise_refs`.

- [ ] **Step 3: Implement the method**

Add to `LodgifyInbox`, immediately after `list_conversations`:

```python
    def summarise_refs(self, refs: set[str]) -> dict[str, dict[str, Any]]:
        """Summarise named conversations, wherever they sit in the archive.

        The Inbox listing enumerates current and upcoming stays only. This is
        how a conversation outside that set -- a Historic one, known because a
        webhook named it -- gets a live summary.

        Costs **one** archive scan for the whole call, plus one thread read per
        requested ref. Resolving each ref on its own would re-page the archive
        every time, and a Historic booking sits near the end of it, so the cost
        would multiply by the number of rows and reach the provider's rate
        limit. Never call `get_conversation` in a loop for this.

        A ref that matches no booking is simply absent from the result: an
        unknown conversation is not an error here, it is one the archive no
        longer explains.
        """
        if not refs:
            return {}

        wanted = [
            booking
            for booking in self.all_bookings(stay_filters=(STAY_FILTER_ALL,))
            if booking.conversation_ref in refs
        ]

        return {
            summary.conversation_ref: summary.to_dict()
            for summary in self.summarise_all(wanted)
        }
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_inbox.py -q`
Expected: PASS — the four new tests plus all pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add app/connectors/lodgify/inbox.py tests/test_inbox.py
git commit -m "feat: summarise named conversations with one archive scan"
```

---

### Task 4: `app/inbox_view.py` — merge, order, limit, enrich

**Files:**
- Create: `app/inbox_view.py`
- Create: `tests/test_inbox_view.py`

**Interfaces:**
- Consumes: `LodgifyInbox.list_conversations`, `LodgifyInbox.summarise_refs` (Task 3); `ConversationActivityStore` (Task 2).
- Produces: `build_inbox(inbox, activity_store, property_slug=None, limit=DEFAULT_LIMIT) -> list[dict[str, Any]]`.

Each returned row is a conversation summary dict plus `preview_unavailable: bool`. That flag is the discriminator the console needs: a null excerpt on a *live* row means the thread could not be read ("No messages could be read."), whereas a null excerpt on a persisted-only row whose enrichment failed means we simply have no preview ("Preview unavailable"). Without it the two states are indistinguishable in the payload.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inbox_view.py`:

```python
"""Merging live conversations with the persisted activity index."""

from app.conversation_activity import ConversationActivityStore
from app.connectors.lodgify.messaging_models import ConversationStatus
from app.connectors.lodgify.refs import conversation_ref_for
from app.inbox_view import build_inbox
from tests.lodgify_fakes import FakeLodgify, booking, message, thread


def live_fake(entries):
    bookings, threads = [], {}

    for booking_id, uid, at, sender in entries:
        bookings.append(booking(booking_id, uid))
        threads[uid] = thread(
            uid, [message(f"m-{uid}", sender, "Invented fixture text.", at)]
        )

    return FakeLodgify(bookings=bookings, threads=threads)


def remember(store, ref, at, status=ConversationStatus.NEEDS_ATTENTION.value):
    store.upsert(
        conversation_ref=ref,
        conversation_fingerprint=f"fp-{ref}",
        status=status,
        last_message_at=at,
        last_message_sender="Renter",
        message_count=2,
        property_slug="renovated-2nd-floor-home",
        source="BookingCom",
        booking_status="Booked",
    )


def refs(rows):
    return [row["conversation_ref"] for row in rows]


def test_a_persisted_historic_conversation_appears_in_the_inbox(database):
    """The whole point: a webhook-known conversation the live scan cannot see."""
    fake = live_fake([(1001, "thread-a", "2026-08-01T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")

    rows = build_inbox(fake.inbox(), store)

    assert refs(rows)[0] == "PH-HISTORIC1"


def test_live_and_persisted_rows_merge(database):
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-01T09:00:00")

    rows = build_inbox(fake.inbox(), store)

    assert set(refs(rows)) == {conversation_ref_for(1001), "PH-HISTORIC1"}


def test_a_conversation_in_both_sources_appears_once(database):
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(1001), "2026-01-01T00:00:00")

    rows = build_inbox(fake.inbox(), store)

    listed = refs(rows)

    assert len(listed) == len(set(listed))
    assert listed.count(conversation_ref_for(1001)) == 1


def test_the_live_row_wins_over_a_persisted_duplicate(database):
    """The live read is authoritative; the index is a snapshot."""
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(1001), "2026-01-01T00:00:00")

    row = build_inbox(fake.inbox(), store)[0]

    assert row["last_message_at"] == "2026-09-02T09:00:00"
    assert row["last_message_sender"] == "Owner"


def test_ordering_is_by_last_message_at_across_both_sources(database):
    fake = live_fake(
        [
            (1001, "thread-a", "2026-09-02T09:00:00", "Owner"),
            (1002, "thread-b", "2026-08-20T09:00:00", "Owner"),
        ]
    )
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")
    remember(store, "PH-HISTORIC2", "2026-08-25T09:00:00")

    assert refs(build_inbox(fake.inbox(), store)) == [
        "PH-HISTORIC1",
        conversation_ref_for(1001),
        "PH-HISTORIC2",
        conversation_ref_for(1002),
    ]


def test_the_limit_is_applied_after_the_merge(database):
    fake = live_fake([(1001, "thread-a", "2026-08-20T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")

    rows = build_inbox(fake.inbox(), store, limit=1)

    assert refs(rows) == ["PH-HISTORIC1"]


def test_a_persisted_only_row_is_enriched_with_a_live_excerpt(database):
    """It survived onto the page, so we read it once to supply a preview."""
    fake = live_fake(
        [
            (1001, "thread-a", "2026-08-01T09:00:00", "Owner"),
            (9001, "thread-hist", "2026-09-03T12:06:33", "Renter"),
        ]
    )
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(9001), "2026-09-03T12:06:33")

    row = build_inbox(fake.inbox(), store)[0]

    assert row["conversation_ref"] == conversation_ref_for(9001)
    assert row["last_message_excerpt"] == "Invented fixture text."
    assert row["preview_unavailable"] is False


def test_a_failed_enrichment_keeps_the_row_and_invents_nothing(database):
    """Fail closed to visible-with-less-detail, never to absent."""
    fake = live_fake([(1001, "thread-a", "2026-08-01T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    # No booking matches this ref, so enrichment cannot resolve it.
    remember(store, "PH-UNREACHBL", "2026-09-03T12:06:33")

    row = build_inbox(fake.inbox(), store)[0]

    assert row["conversation_ref"] == "PH-UNREACHBL"
    assert row["last_message_at"] == "2026-09-03T12:06:33"
    assert row["last_message_excerpt"] is None
    assert row["preview_unavailable"] is True


def test_enrichment_pages_the_archive_once_for_the_whole_page(database):
    """Historic rows must not multiply the archive paging cost."""
    fake = live_fake(
        [
            (1001, "thread-a", "2026-08-01T09:00:00", "Owner"),
            (9001, "thread-h1", "2026-09-03T10:00:00", "Renter"),
            (9002, "thread-h2", "2026-09-03T11:00:00", "Renter"),
            (9003, "thread-h3", "2026-09-03T12:00:00", "Renter"),
        ]
    )
    store = ConversationActivityStore(database=database)

    for booking_id, at in ((9001, "10"), (9002, "11"), (9003, "12")):
        remember(store, conversation_ref_for(booking_id), f"2026-09-03T{at}:00:00")

    before = len(fake.booking_reads)

    build_inbox(fake.inbox(), store)

    # The live listing scans once per stay filter; enrichment adds exactly one
    # more archive scan for all three persisted-only rows together.
    from app.connectors.lodgify.messaging_client import INBOX_STAY_FILTERS

    assert len(fake.booking_reads) - before == len(INBOX_STAY_FILTERS) + 1


def test_enrichment_updates_the_index_when_it_finds_newer_activity(database):
    fake = live_fake(
        [
            (1001, "thread-a", "2026-08-01T09:00:00", "Owner"),
            (9001, "thread-hist", "2026-09-03T12:06:33", "Renter"),
        ]
    )
    store = ConversationActivityStore(database=database)

    remember(store, conversation_ref_for(9001), "2026-09-01T00:00:00")

    build_inbox(fake.inbox(), store)

    assert store.for_conversation(conversation_ref_for(9001)).last_message_at == (
        "2026-09-03T12:06:33"
    )


def test_the_live_scan_upserts_the_index(database):
    """One durable index regardless of trigger."""
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    build_inbox(fake.inbox(), store)

    stored = store.for_conversation(conversation_ref_for(1001))

    assert stored is not None
    assert stored.last_message_at == "2026-09-02T09:00:00"


def test_a_historic_row_survives_ordinary_polling(database):
    """Polling must not evict what only a webhook could have told us."""
    fake = live_fake([(1001, "thread-a", "2026-09-02T09:00:00", "Owner")])
    store = ConversationActivityStore(database=database)

    remember(store, "PH-HISTORIC1", "2026-09-03T12:06:33")

    build_inbox(fake.inbox(), store)
    build_inbox(fake.inbox(), store)

    assert store.for_conversation("PH-HISTORIC1") is not None
    assert "PH-HISTORIC1" in refs(build_inbox(fake.inbox(), store))
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_inbox_view.py -q`
Expected: FAIL — `No module named 'app.inbox_view'`.

- [ ] **Step 3: Implement the view**

Create `app/inbox_view.py`:

```python
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

from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import DEFAULT_LIMIT, LodgifyInbox
from app.conversation_activity import ConversationActivityStore


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

    by_ref = {row["conversation_ref"]: dict(row, preview_unavailable=False) for row in live}

    for activity in activity_store.all_activity():
        if activity.conversation_ref in by_ref:
            # Live wins: a snapshot never overrides something just read.
            continue

        if property_slug is not None and activity.property_slug != property_slug:
            continue

        by_ref[activity.conversation_ref] = dict(
            activity.to_row(),
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


def _enrich(
    inbox: LodgifyInbox,
    store: ConversationActivityStore,
    page: list[dict[str, Any]],
) -> None:
    """Supply a live excerpt for persisted-only rows that made the page.

    One archive scan for all of them together -- see
    `LodgifyInbox.summarise_refs`. A row we cannot read stays visible with no
    preview; it is never dropped and nothing is invented for it.
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
            continue

        page[index] = dict(fresh, preview_unavailable=False)

        _remember(store, fresh)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_inbox_view.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add app/inbox_view.py tests/test_inbox_view.py
git commit -m "feat: merge live and persisted conversations into the Inbox"
```

---

### Task 5: Wire the route and the webhook path

**Files:**
- Modify: `app/main.py` (module wiring; `get_inbox`; `refresh_conversation_safely`)
- Modify: `app/models.py` (add `preview_unavailable` to the inbox row model)
- Modify: `tests/test_inbox_api.py`

**Interfaces:**
- Consumes: `build_inbox` (Task 4), `ConversationActivityStore` (Task 2).
- Produces: `GET /inbox` rows carrying `preview_unavailable: bool`; a module-level `activity_store`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inbox_api.py`, following that file's existing client/fixture pattern:

```python
def test_inbox_rows_expose_the_preview_flag(client):
    rows = client.get("/inbox").json()["conversations"]

    assert all("preview_unavailable" in row for row in rows)


def test_a_webhook_known_conversation_reaches_the_api(client, database):
    """End to end: what a webhook remembered is listed by the route."""
    from app.conversation_activity import ConversationActivityStore

    ConversationActivityStore(database=database).upsert(
        conversation_ref="PH-HISTORIC1",
        conversation_fingerprint="fp-1",
        status="needs_attention",
        last_message_at="2026-09-03T12:06:33",
        last_message_sender="Renter",
        message_count=2,
        property_slug="renovated-2nd-floor-home",
        source="BookingCom",
        booking_status="Booked",
    )

    rows = client.get("/inbox").json()["conversations"]

    assert rows[0]["conversation_ref"] == "PH-HISTORIC1"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_inbox_api.py -q`
Expected: FAIL — no `preview_unavailable` key.

- [ ] **Step 3: Wire it**

In `app/models.py`, add to the inbox-row model (beside `last_message_excerpt`):

```python
    # False on a live row; True on a row we remembered but could not re-read.
    preview_unavailable: bool = False
```

In `app/main.py`, beside the existing `draft_store` wiring:

```python
from app.conversation_activity import ConversationActivityStore
from app.inbox_view import build_inbox

activity_store = ConversationActivityStore(database=database)
```

In `get_inbox`, replace the `inbox.list_conversations(...)` call with:

```python
        conversations = build_inbox(
            inbox,
            activity_store,
            property_slug=property_slug,
            limit=limit,
        )
```

Leave the surrounding `except (ValueError, TypeError)` / `LodgifyConfigurationError` / `LodgifyUnavailable` handling and the draft-attachment loop below it exactly as they are.

In `refresh_conversation_safely`, after `conversation_refresh.process(conversation_ref)` succeeds, record activity so a Historic conversation becomes listable:

```python
    try:
        conversation_refresh.process(conversation_ref)

        # The webhook is why a Historic conversation is listable at all: the
        # Inbox scan will never enumerate it, so what we learned here is the
        # only record that it moved.
        if lodgify_inbox is not None:
            summary = lodgify_inbox.summarise_refs({conversation_ref}).get(
                conversation_ref
            )

            if summary is not None:
                activity_store.upsert(
                    conversation_ref=summary["conversation_ref"],
                    conversation_fingerprint=summary.get("fingerprint") or "",
                    status=summary["status"],
                    last_message_at=summary.get("last_message_at"),
                    last_message_sender=summary.get("last_message_sender"),
                    message_count=summary.get("message_count") or 0,
                    property_slug=summary.get("property_slug"),
                    source=summary.get("source"),
                    booking_status=summary.get("booking_status"),
                )

    except Exception:
        logger.exception("background refresh failed for %s", conversation_ref)
```

**Do not otherwise modify `conversation_refresh.py` or `drafts.py`.**

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_inbox_api.py tests/test_lodgify_webhooks.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/models.py tests/test_inbox_api.py
git commit -m "feat: serve merged Inbox rows and index webhook activity"
```

---

### Task 6: Console — "Preview unavailable"

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/pages/InboxPage.tsx:155-165`
- Modify: `frontend/src/test/factories.ts`
- Modify: `frontend/src/pages/InboxPage.test.tsx`

**Interfaces:**
- Consumes: `preview_unavailable: boolean` on the inbox row (Task 5).

- [ ] **Step 1: Write the failing test**

Append to `frontend/src/pages/InboxPage.test.tsx`:

```tsx
it('shows a neutral notice when a remembered conversation has no preview', async () => {
  mockInbox([
    conversationSummary({
      conversation_ref: 'PH-HISTORIC1',
      last_message_excerpt: null,
      preview_unavailable: true,
    }),
  ])

  render(<InboxPage />, { wrapper: Wrapper })

  expect(await screen.findByText('Preview unavailable')).toBeInTheDocument()
  expect(screen.queryByText('No messages could be read.')).not.toBeInTheDocument()
})

it('still distinguishes an unreadable live thread', async () => {
  mockInbox([
    conversationSummary({
      last_message_excerpt: null,
      preview_unavailable: false,
    }),
  ])

  render(<InboxPage />, { wrapper: Wrapper })

  expect(await screen.findByText('No messages could be read.')).toBeInTheDocument()
})
```

Match the existing file's own import and mock helpers rather than inventing new ones.

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npm run test -- InboxPage`
Expected: FAIL — "Preview unavailable" is not rendered.

- [ ] **Step 3: Implement**

In `frontend/src/api/types.ts`, add to the conversation-summary type:

```ts
  preview_unavailable: boolean
```

In `frontend/src/test/factories.ts`, add `preview_unavailable: false` to the conversation-summary factory default.

In `frontend/src/pages/InboxPage.tsx`, replace the excerpt block:

```tsx
              <div className="conversation-excerpt">
                {row.last_message_excerpt ? (
                  <>
                    <span className="faint">
                      {row.last_message_sender === 'Renter' ? 'Guest: ' : 'You: '}
                    </span>
                    {row.last_message_excerpt}
                  </>
                ) : row.preview_unavailable ? (
                  <span className="faint">Preview unavailable</span>
                ) : (
                  <span className="faint">No messages could be read.</span>
                )}
              </div>
```

- [ ] **Step 4: Run the frontend gates**

```bash
cd frontend && npm run test && npm run typecheck && npm run lint && npm run build
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat: distinguish a missing preview from an unreadable thread"
```

---

### Task 7: Full verification

**Files:** none modified.

- [ ] **Step 1: Backend gates and database-isolation proof**

```bash
uv run ruff format .
uv run ruff check .
md5 -q agentops.db > /tmp/db_before
uv run pytest -q
md5 -q agentops.db > /tmp/db_after
diff /tmp/db_before /tmp/db_after && echo "agentops.db UNTOUCHED"
```
Expected: format clean, check clean, all tests pass, hashes identical. The automated proof is `tests/test_migrations.py::test_the_development_database_is_untouched` plus `tests/test_conversation_activity.py::test_the_store_never_touches_the_development_database`; the hash diff is the belt-and-braces manual check.

- [ ] **Step 2: Alembic upgrade checks**

```bash
uv run alembic heads          # exactly one head
TMP=$(mktemp -d)
AGENTOPS_DATABASE_URL="sqlite:///$TMP/fresh.db" uv run alembic upgrade head
AGENTOPS_DATABASE_URL="sqlite:///$TMP/fresh.db" uv run alembic current
```
Expected: one head; fresh database reaches head; `conversation_activity` present and empty. For "existing DB → head", rely on `test_upgrading_a_populated_baseline_database_preserves_data`; **do not run a migration against `agentops.db`** as part of verification.

- [ ] **Step 3: Live read-only verification**

Write a throwaway script under the scratchpad (never committed) that loads `.env`, builds a real `LodgifyInbox` and a `ConversationActivityStore` against a **temporary** database, and:

1. calls `build_inbox(...)` and prints safe metadata only — `conversation_ref`, property slug, source, `last_message_at`, `last_message_sender`, status, `preview_unavailable`, rank;
2. asserts no duplicate `conversation_ref`;
3. seeds one activity row for a known Historic conversation, re-runs `build_inbox`, and confirms it appears in rank order and is enriched;
4. counts provider requests to confirm enrichment adds exactly one archive scan.

GETs only. **Never print** guest name, email, phone, message body, `booking_id`, `thread_uid`, raw payload or the API key. Do not send a message. Do not point the script at `agentops.db`.

- [ ] **Step 4: Report and stop**

Report root-cause coverage, files changed, tests added, exact gate output, and live results. Do not commit the spec or plan without being asked.

---

## Self-review

**Spec coverage.** Storage → Task 1–2. Two writers, one index → Task 4 (`_remember` on the live scan) and Task 5 (webhook). Merge/order/limit → Task 4. Enrichment with one shared scan → Task 3 + Task 4 `_enrich`. Failure behaviour → Task 4. Retention (no ageing-out) → Task 4 `test_a_historic_row_survives_ordinary_polling`; no deletion path is implemented anywhere, which is the requirement. Migration → Task 1. Console → Task 6. All 13 spec tests are present, plus `preview_unavailable` coverage the spec implied.

**Placeholders.** None: every code step carries real code, every test step real assertions.

**Type consistency.** `summarise_refs(refs: set[str]) -> dict[str, dict]` is defined in Task 3 and consumed identically in Tasks 4 and 5. `ConversationActivityStore.upsert` keyword names match at all three call sites. `preview_unavailable` is a `bool` in `app/models.py`, `types.ts`, the factory and both test files.

**Known gap, deliberate.** `_remember` writes on every live row each poll, so `last_refreshed_at` moves ~152 rows per poll. Acceptable for V1 on SQLite; if it shows up in profiling, skip the write when the fingerprint is unchanged. Not optimised pre-emptively.
