"""What AgentGuard has already worked out about a conversation.

A row here is the outcome of processing one *state* of one conversation: a
prepared reply, a decision that no reply is needed, or an admission that
drafting failed. The owner opens the Inbox and the work is done.

The fingerprint is what keeps that honest. A prepared reply answers a
conversation as it stood at a moment in time, and if the guest writes again
afterwards the reply is answering a question that has moved on. The dangerous
version of that is an operator seeing a draft already written and sending it
without re-reading the thread -- so staleness is **derived on read** by
comparing fingerprints, never trusted from a stored flag.

The same fingerprint is also the cost control. One conversation state gets at
most one model call, however many times a webhook fires or a poll comes round.

Nothing here sends anything.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select

from app.connectors.lodgify.messaging_models import conversation_fingerprint
from app.database import Database, get_database
from app.db_models import ConversationDraftRecord

__all__ = [
    "ConversationDraft",
    "DraftStatus",
    "DraftStore",
    "conversation_fingerprint",
    "draft_ref_for",
]


class DraftStatus(str, Enum):
    """The outcome of processing one conversation state.

    `STALE` is deliberately absent: it is not an outcome anything decides, it is
    what a sendable draft *becomes* when the conversation moves on, and it is
    computed by `status_for`. Storing it would allow a row to be stale in the
    database and fresh on screen.
    """

    DRAFT_READY = "DRAFT_READY"
    EDITED = "EDITED"
    NO_REPLY_NEEDED = "NO_REPLY_NEEDED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    SENT = "SENT"
    DISCARDED = "DISCARDED"


STALE = "STALE"

# Outcomes that carry text somebody could send. Only these can go stale, because
# only these are dangerous when the conversation has moved on.
SENDABLE_STATUSES = (DraftStatus.DRAFT_READY.value, DraftStatus.EDITED.value)

# Outcomes that mean the work for this state is done, so a refresh should not
# redo it. NEEDS_HUMAN_REVIEW is *not* here, because the status covers two
# different things -- see `is_settled`.
SETTLED_STATUSES = (
    DraftStatus.DRAFT_READY.value,
    DraftStatus.EDITED.value,
    DraftStatus.NO_REPLY_NEEDED.value,
    DraftStatus.SENT.value,
    DraftStatus.DISCARDED.value,
)


def draft_ref_for(conversation_ref: str, fingerprint: str) -> str:
    """One outcome per conversation state.

    Keying on the fingerprint is what makes the whole pipeline idempotent: four
    identical webhook deliveries and a poll all compute the same ref, so the
    second one through finds the first one's work already done.
    """
    material = f"{conversation_ref}\x1f{fingerprint}"

    return "dr-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ConversationDraft:
    """One processing outcome, as everything above the store sees it."""

    draft_ref: str
    conversation_ref: str
    property_slug: str | None
    conversation_fingerprint: str
    subject: str | None
    message: str | None
    status: str
    detail: str | None
    source_run_id: str | None
    created_at: str
    updated_at: str
    edited_at: str | None
    sent_at: str | None

    def is_current(self, current_fingerprint: str | None) -> bool:
        """Whether this outcome describes the conversation as it stands now."""
        if not current_fingerprint:
            return True

        return current_fingerprint == self.conversation_fingerprint

    def carries_text(self) -> bool:
        """Whether this row holds a reply a person could actually send.

        An escalated draft carries text too: policy says offer the guest the
        hour that *is* available and let the owner decide about the rest, so
        the wording exists and must be shown -- and must be able to go stale,
        for exactly the same reason a ready draft must.
        """
        return bool(self.message) and self.status in (
            *SENDABLE_STATUSES,
            DraftStatus.NEEDS_HUMAN_REVIEW.value,
        )

    def is_settled(self) -> bool:
        """Whether processing this conversation state produced a keepable result.

        NEEDS_HUMAN_REVIEW means two different things and this is where they
        part. With text, it is finished work waiting on a person -- redoing it
        would spend a model call to overwrite a reply the owner may already be
        reading. Without text, drafting failed, and the next poll should retry.
        """
        if self.status == DraftStatus.NEEDS_HUMAN_REVIEW.value:
            return bool(self.message)

        return self.status in SETTLED_STATUSES

    def status_for(self, current_fingerprint: str | None) -> str:
        """The status as it actually is, given the live conversation.

        Only a *sendable* outcome becomes STALE. A superseded
        `NO_REPLY_NEEDED` is not dangerous, merely out of date, and calling it
        stale would put a warning on a screen where nothing is wrong.
        """
        if self.carries_text() and not self.is_current(current_fingerprint):
            return STALE

        return self.status

    @property
    def sendable_text(self) -> tuple[str, str] | None:
        """The exact subject and message, or nothing if this is not a draft."""
        if self.status not in SENDABLE_STATUSES:
            return None

        if not self.subject or not self.message:
            return None

        return self.subject, self.message

    def to_dict(self, current_fingerprint: str | None = None) -> dict[str, Any]:
        return {
            "draft_ref": self.draft_ref,
            "conversation_ref": self.conversation_ref,
            "property_slug": self.property_slug,
            "status": self.status_for(current_fingerprint),
            "stored_status": self.status,
            "is_current": self.is_current(current_fingerprint),
            "subject": self.subject,
            "message": self.message,
            "detail": self.detail,
            "source_run_id": self.source_run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "edited_at": self.edited_at,
            "sent_at": self.sent_at,
        }


def to_draft(record: ConversationDraftRecord) -> ConversationDraft:
    return ConversationDraft(
        draft_ref=record.draft_ref,
        conversation_ref=record.conversation_ref,
        property_slug=record.property_slug,
        conversation_fingerprint=record.conversation_fingerprint,
        subject=record.subject,
        message=record.message,
        status=record.status,
        detail=record.detail,
        source_run_id=record.source_run_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        edited_at=record.edited_at,
        sent_at=record.sent_at,
    )


class DraftStore:
    """Persistence for processing outcomes.

    No provider identifier is stored: a conversation_ref is already opaque, and
    there is no column here for a booking id, a thread uid or a guest's name.
    """

    def __init__(self, database: Database | None = None) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database or get_database()

    def record_outcome(
        self,
        conversation_ref: str,
        conversation_fingerprint: str,
        status: DraftStatus,
        property_slug: str | None = None,
        subject: str | None = None,
        message: str | None = None,
        detail: str | None = None,
        source_run_id: str | None = None,
    ) -> tuple[ConversationDraft, bool]:
        """Store the result of processing one conversation state.

        Returns (outcome, created). An existing row for the same state is
        returned untouched rather than overwritten -- if a person has edited
        that draft, a later automatic pass must not silently replace their
        words. The exception is a previous failure: `NEEDS_HUMAN_REVIEW` is
        replaced, because a retry that succeeded is strictly better news.
        """
        ref = draft_ref_for(conversation_ref, conversation_fingerprint)

        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            existing = session.scalar(
                select(ConversationDraftRecord).where(
                    ConversationDraftRecord.draft_ref == ref
                )
            )

            if existing is not None:
                if existing.status != DraftStatus.NEEDS_HUMAN_REVIEW.value:
                    return to_draft(existing), False

                existing.status = status.value
                existing.subject = subject
                existing.message = message
                existing.detail = detail
                existing.source_run_id = source_run_id
                existing.updated_at = now

                session.commit()

                return to_draft(existing), False

            record = ConversationDraftRecord(
                draft_ref=ref,
                conversation_ref=conversation_ref,
                property_slug=property_slug,
                conversation_fingerprint=conversation_fingerprint,
                subject=subject,
                message=message,
                status=status.value,
                detail=detail,
                source_run_id=source_run_id,
                created_at=now,
                updated_at=now,
            )

            session.add(record)
            session.commit()

            return to_draft(record), True

    def edit(
        self,
        draft_ref: str,
        subject: str | None = None,
        message: str | None = None,
    ) -> ConversationDraft:
        """Keep the operator's wording.

        The fingerprint is untouched, so an edited draft goes stale exactly like
        a generated one when the guest writes again. The newer conversation wins
        over anybody's earlier wording, a person's included.
        """
        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            record = session.scalar(
                select(ConversationDraftRecord).where(
                    ConversationDraftRecord.draft_ref == draft_ref
                )
            )

            if record is None:
                raise ValueError(f"Unknown draft_ref: {draft_ref!r}")

            if record.status in (
                DraftStatus.SENT.value,
                DraftStatus.DISCARDED.value,
            ):
                raise ValueError(f"A {record.status} draft cannot be edited.")

            if subject is not None:
                record.subject = subject

            if message is not None:
                record.message = message

            record.status = DraftStatus.EDITED.value
            record.edited_at = now
            record.updated_at = now

            session.commit()

            return to_draft(record)

    def replace(
        self,
        draft_ref: str,
        status: DraftStatus,
        subject: str | None = None,
        message: str | None = None,
        detail: str | None = None,
        source_run_id: str | None = None,
    ) -> ConversationDraft:
        """Overwrite an outcome for the same conversation state.

        Only reached by an explicit Regenerate. Nothing automatic replaces a
        settled outcome, which is what stops a poll spending a second model call
        on an unchanged thread -- or overwriting wording a person has edited.
        """
        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            record = session.scalar(
                select(ConversationDraftRecord).where(
                    ConversationDraftRecord.draft_ref == draft_ref
                )
            )

            if record is None:
                raise ValueError(f"Unknown draft_ref: {draft_ref!r}")

            record.status = status.value
            record.subject = subject
            record.message = message
            record.detail = detail
            record.source_run_id = source_run_id
            record.edited_at = None
            record.updated_at = now

            session.commit()

            return to_draft(record)

    def mark_sent(self, draft_ref: str) -> ConversationDraft:
        """Retire a draft after a *confirmed* send, and only then."""
        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            record = session.scalar(
                select(ConversationDraftRecord).where(
                    ConversationDraftRecord.draft_ref == draft_ref
                )
            )

            if record is None:
                raise ValueError(f"Unknown draft_ref: {draft_ref!r}")

            record.status = DraftStatus.SENT.value
            record.sent_at = now
            record.updated_at = now

            session.commit()

            return to_draft(record)

    def current_for(self, conversation_ref: str) -> ConversationDraft | None:
        """The most recent outcome for a conversation, whatever its state."""
        with self.database.session() as session:
            record = session.scalars(
                select(ConversationDraftRecord)
                .where(ConversationDraftRecord.conversation_ref == conversation_ref)
                .order_by(ConversationDraftRecord.id.desc())
                .limit(1)
            ).first()

            return to_draft(record) if record is not None else None

    def for_state(
        self,
        conversation_ref: str,
        fingerprint: str,
    ) -> ConversationDraft | None:
        """Work already done for exactly this conversation state.

        The idempotency check: if this returns something settled, there is no
        reason to analyse or call the model again.
        """
        ref = draft_ref_for(conversation_ref, fingerprint)

        with self.database.session() as session:
            record = session.scalar(
                select(ConversationDraftRecord).where(
                    ConversationDraftRecord.draft_ref == ref
                )
            )

            return to_draft(record) if record is not None else None

    def get(self, draft_ref: str) -> ConversationDraft | None:
        with self.database.session() as session:
            record = session.scalar(
                select(ConversationDraftRecord).where(
                    ConversationDraftRecord.draft_ref == draft_ref
                )
            )

            return to_draft(record) if record is not None else None

    def latest_by_conversation(self) -> dict[str, ConversationDraft]:
        """The current outcome for every conversation, for the Inbox list."""
        with self.database.session() as session:
            records = session.scalars(
                select(ConversationDraftRecord).order_by(ConversationDraftRecord.id)
            ).all()

        # Ascending id, so the last write for a conversation wins.
        return {record.conversation_ref: to_draft(record) for record in records}

    def count(self) -> int:
        with self.database.session() as session:
            return len(session.scalars(select(ConversationDraftRecord.id)).all())
