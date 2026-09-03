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
