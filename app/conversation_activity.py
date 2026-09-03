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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, nulls_last, select

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

        No excerpt key is present: none is stored here, so there is nothing
        to project. A caller enriching this row for display adds a preview
        separately, or leaves it absent.
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
            "message_count": self.message_count,
        }


# SQLite refuses a statement with more than a few hundred bound parameters,
# and an `IN` clause over every current-and-upcoming conversation is already
# in the hundreds. Chunking keeps these queries correct as the account grows.
_IN_CHUNK = 400


def _chunked(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), _IN_CHUNK):
        yield values[start : start + _IN_CHUNK]


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

            session.commit()

            return _read(record)

    def all_activity(self) -> list[ConversationActivity]:
        """Every row. Kept for callers that genuinely need the whole table.

        Not on the Inbox read path: ordering a page loads
        `ordered_activity(limit)` instead, which the database bounds rather
        than Python.
        """
        with self.database.session() as session:
            return [
                _read(record)
                for record in session.scalars(select(ConversationActivityRecord))
            ]

    def ordered_activity(
        self,
        limit: int,
        property_slug: str | None = None,
    ) -> list[ConversationActivity]:
        """The most recently active conversations, newest first.

        This is what the Inbox orders by, and it costs zero provider calls --
        which is the entire point of the index. `last_message_at` is indexed
        (`ix_conversation_activity_last_message_at`), so this is a bounded
        index scan rather than the whole table loaded into Python.

        `NULLS LAST` is stated rather than assumed: SQLite happens to put nulls
        last on a descending sort and PostgreSQL happens to put them first, and
        a conversation whose last-message time is unknown belongs at the bottom
        under both. Ties break on `conversation_ref` ascending, matching the
        two-pass sort this replaced, so the order is deterministic.

        `property_slug` narrows before the limit is applied. Filtering a
        globally-ordered page afterwards would answer a different question --
        the twenty newest conversations that happen to be at this property,
        rather than this property's twenty newest.
        """
        statement = select(ConversationActivityRecord)

        if property_slug is not None:
            statement = statement.where(
                ConversationActivityRecord.property_slug == property_slug
            )

        statement = statement.order_by(
            nulls_last(ConversationActivityRecord.last_message_at.desc()),
            ConversationActivityRecord.conversation_ref.asc(),
        ).limit(limit)

        with self.database.session() as session:
            return [_read(record) for record in session.scalars(statement)]

    def least_recently_refreshed(
        self,
        limit: int,
        exclude: Iterable[str] = (),
    ) -> list[ConversationActivity]:
        """The rows whose metadata is oldest, for the rotating sweep.

        The provider offers no way to learn which threads changed -- a booking
        row carries no last-message field and its `updated_at` does not move
        when a message arrives -- so a message that arrives with no webhook is
        found by re-reading. Re-reading everything is the request burst this
        design exists to remove, so the sweep takes the oldest slice and comes
        back for the next one.

        The consequence is worth stating where it can be read: every
        conversation is re-read within one full cycle, and that cycle grows
        linearly with the number of conversations in the account.
        """
        skipped = set(exclude)

        statement = select(ConversationActivityRecord)

        if skipped:
            statement = statement.where(
                ConversationActivityRecord.conversation_ref.notin_(skipped)
            )

        statement = statement.order_by(
            ConversationActivityRecord.last_refreshed_at.asc(),
            ConversationActivityRecord.conversation_ref.asc(),
        ).limit(limit)

        with self.database.session() as session:
            return [_read(record) for record in session.scalars(statement)]

    def known_refs(self, refs: Iterable[str]) -> set[str]:
        """Which of these conversations the index has already seen.

        The cheap half of cold start: the booking scan says which
        conversations exist, this says which have never been read, and the
        difference is what a seeding batch works through. Only the reference
        column is selected -- nothing else is needed to answer the question.
        """
        wanted = list(dict.fromkeys(refs))

        if not wanted:
            return set()

        found: set[str] = set()

        with self.database.session() as session:
            for chunk in _chunked(wanted):
                found.update(
                    session.scalars(
                        select(ConversationActivityRecord.conversation_ref).where(
                            ConversationActivityRecord.conversation_ref.in_(chunk)
                        )
                    )
                )

        return found

    def oldest_refreshed_at(self, refs: Iterable[str]) -> str | None:
        """The oldest `last_refreshed_at` among these rows, or None.

        How the Inbox reports its own staleness. None means no row was found,
        which is not staleness -- an empty page is not a behind one.
        """
        wanted = list(dict.fromkeys(refs))

        if not wanted:
            return None

        oldest: str | None = None

        with self.database.session() as session:
            for chunk in _chunked(wanted):
                value = session.scalar(
                    select(
                        func.min(ConversationActivityRecord.last_refreshed_at)
                    ).where(ConversationActivityRecord.conversation_ref.in_(chunk))
                )

                if value is not None and (oldest is None or value < oldest):
                    oldest = value

        return oldest

    def for_conversation(self, conversation_ref: str) -> ConversationActivity | None:
        with self.database.session() as session:
            record = session.scalar(
                select(ConversationActivityRecord).where(
                    ConversationActivityRecord.conversation_ref == conversation_ref
                )
            )

            return _read(record) if record is not None else None
