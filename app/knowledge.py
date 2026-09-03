"""Owner-approved Priyanka Homes knowledge.

The gap this closes: historical retrieval gave the model the owner's *voice* but
not the owner's *facts*, because a past reply is never authoritative. Asked how
to reach the property from the airport, the model retrieved four good precedents
and still answered "I'll check" -- correctly, since directions are a
property-specific fact and an old reply cannot establish one.

This module is the controlled path from "the owner has answered this fifteen
times" to "this is what Priyanka Homes says". The control is a person:

    historical examples  ->  PROPOSED  ->  [human approves]  ->  APPROVED

**Nothing here promotes anything.** Distillation writes PROPOSED rows and stops.
Only `approve()` -- reached from an authenticated, ADMIN-gated route -- makes a
row authoritative, and it records who did it. Frequency is not truth: twenty old
messages saying parking is free do not make parking free today.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import HospitalityKnowledgeRecord
from app.knowledge_topics import GUEST_FACING, INTERNAL_OPERATION


class KnowledgeStatus(str, Enum):
    """Where a piece of knowledge sits in its review lifecycle.

    Only APPROVED reaches a guest-facing draft. PROPOSED is a suggestion,
    REJECTED is a decision worth keeping so the same candidate is not
    re-litigated, and SUPERSEDED preserves what a rule used to say.
    """

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class KnowledgeSource(str, Enum):
    HISTORICAL_DISTILLATION = "HISTORICAL_DISTILLATION"
    MANUAL = "MANUAL"


GLOBAL_SCOPE = "global"

MAX_TITLE_LENGTH = 200

MAX_CONTENT_LENGTH = 800


def knowledge_ref_for(
    property_slug: str | None,
    topic: str,
    title: str,
    discriminator: str | None = None,
) -> str:
    """Stable identity for one rule.

    Scope plus topic plus title: re-running distillation over the same evidence
    finds the existing row rather than proposing a duplicate for the owner to
    review twice.

    `discriminator` exists for supersession. A replacement usually keeps the
    same title and changes only the wording, which would otherwise collide with
    the very row it replaces -- so the new version mixes its content in and gets
    an identity of its own, leaving the original row intact as history.
    """
    parts = [property_slug or GLOBAL_SCOPE, topic, title.strip().lower()]

    if discriminator is not None:
        parts.append(discriminator)

    material = "\x1f".join(parts)

    return "kn-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class KnowledgeItem:
    """One rule, as everything above the store sees it."""

    knowledge_ref: str
    property_slug: str | None
    topic: str
    title: str
    content: str
    status: str
    source_type: str
    audience: str
    safety_status: str
    safety_reasons: tuple[str, ...]
    reason: str | None
    evidence_count: int
    evidence_property_count: int
    evidence_refs: tuple[str, ...]
    first_observed_at: str | None
    last_observed_at: str | None
    created_at: str
    updated_at: str
    decided_at: str | None
    decided_by_user_id: str | None

    @property
    def scope(self) -> str:
        return self.property_slug or GLOBAL_SCOPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_ref": self.knowledge_ref,
            "property_slug": self.property_slug,
            "scope": self.scope,
            "topic": self.topic,
            "title": self.title,
            "content": self.content,
            "status": self.status,
            "source_type": self.source_type,
            "audience": self.audience,
            "safety_status": self.safety_status,
            "safety_reasons": list(self.safety_reasons),
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "evidence_property_count": self.evidence_property_count,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "decided_at": self.decided_at,
            "decided_by_user_id": self.decided_by_user_id,
        }

    def for_drafting(self) -> dict[str, Any]:
        """The projection a model sees. Review metadata is not its business."""
        return {
            "topic": self.topic,
            "scope": self.scope,
            "title": self.title,
            "content": self.content,
        }


def to_item(record: HospitalityKnowledgeRecord) -> KnowledgeItem:
    return KnowledgeItem(
        knowledge_ref=record.knowledge_ref,
        property_slug=record.property_slug,
        topic=record.topic,
        title=record.title,
        content=record.content,
        status=record.status,
        source_type=record.source_type,
        audience=record.audience,
        safety_status=record.safety_status,
        safety_reasons=tuple(record.safety_reasons),
        reason=record.reason,
        evidence_count=record.evidence_count,
        evidence_property_count=record.evidence_property_count,
        evidence_refs=tuple(record.evidence_refs),
        first_observed_at=record.first_observed_at,
        last_observed_at=record.last_observed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        decided_at=record.decided_at,
        decided_by_user_id=record.decided_by_user_id,
    )


def validate_title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("title must be a non-empty string.")

    if len(value) > MAX_TITLE_LENGTH:
        raise ValueError(f"title must be {MAX_TITLE_LENGTH} characters or fewer.")

    return value.strip()


def validate_content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("content must be a non-empty string.")

    if len(value) > MAX_CONTENT_LENGTH:
        raise ValueError(f"content must be {MAX_CONTENT_LENGTH} characters or fewer.")

    return value.strip()


class KnowledgeStore:
    """Persistence and lifecycle for hospitality knowledge."""

    def __init__(self, database: Database | None = None) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database or get_database()

    # -- writes ------------------------------------------------------------

    def propose(
        self,
        property_slug: str | None,
        topic: str,
        title: str,
        content: str,
        source_type: str = KnowledgeSource.HISTORICAL_DISTILLATION.value,
        audience: str = INTERNAL_OPERATION,
        safety_status: str = "SAFE",
        safety_reasons: tuple[str, ...] = (),
        reason: str | None = None,
        evidence_refs: tuple[str, ...] = (),
        evidence_property_count: int = 0,
        first_observed_at: str | None = None,
        last_observed_at: str | None = None,
    ) -> tuple[KnowledgeItem, bool]:
        """Record a candidate. Always PROPOSED -- never approved here.

        Returns (item, created). Re-proposing an identical rule refreshes its
        evidence rather than queueing the same decision twice, and a candidate
        the owner already decided on is left alone: a rejection is an answer,
        and re-proposing it would make the queue argue with the reviewer.
        """
        checked_title = validate_title(title)
        checked_content = validate_content(content)

        ref = knowledge_ref_for(property_slug, topic, checked_title)

        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            existing = session.scalar(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.knowledge_ref == ref
                )
            )

            if existing is not None:
                if existing.status == KnowledgeStatus.PROPOSED.value:
                    existing.content = checked_content
                    existing.audience = audience
                    existing.safety_status = safety_status
                    existing.safety_reasons_json = json.dumps(list(safety_reasons))
                    existing.reason = reason
                    existing.evidence_count = len(evidence_refs)
                    existing.evidence_property_count = evidence_property_count
                    existing.evidence_refs_json = json.dumps(list(evidence_refs))
                    existing.first_observed_at = first_observed_at
                    existing.last_observed_at = last_observed_at
                    existing.updated_at = now

                    session.commit()

                return to_item(existing), False

            record = HospitalityKnowledgeRecord(
                knowledge_ref=ref,
                property_slug=property_slug,
                topic=topic,
                title=checked_title,
                content=checked_content,
                status=KnowledgeStatus.PROPOSED.value,
                source_type=source_type,
                audience=audience,
                safety_status=safety_status,
                safety_reasons_json=json.dumps(list(safety_reasons)),
                reason=reason,
                evidence_count=len(evidence_refs),
                evidence_property_count=evidence_property_count,
                evidence_refs_json=json.dumps(list(evidence_refs)),
                first_observed_at=first_observed_at,
                last_observed_at=last_observed_at,
                created_at=now,
                updated_at=now,
            )

            session.add(record)
            session.commit()

            return to_item(record), True

    def decide(
        self,
        knowledge_ref: str,
        status: KnowledgeStatus,
        actor_user_id: str,
    ) -> KnowledgeItem:
        """Approve, reject or supersede. The actor is always recorded."""
        if status not in (
            KnowledgeStatus.APPROVED,
            KnowledgeStatus.REJECTED,
            KnowledgeStatus.SUPERSEDED,
        ):
            raise ValueError(f"{status} is not a decision.")

        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            record = session.scalar(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.knowledge_ref == knowledge_ref
                )
            )

            if record is None:
                raise ValueError(f"Unknown knowledge_ref: {knowledge_ref!r}")

            record.status = status.value
            record.decided_at = now
            record.decided_by_user_id = actor_user_id
            record.updated_at = now

            session.commit()

            return to_item(record)

    def create_manual(
        self,
        property_slug: str | None,
        topic: str,
        title: str,
        content: str,
        audience: str,
        actor_user_id: str,
    ) -> KnowledgeItem:
        """Author a rule directly, already APPROVED.

        Distillation proposes because a model guessed; an owner writing a rule
        by hand *is* the review step, so requiring them to approve their own
        sentence a moment later would be ceremony rather than a control. The
        governance that matters is unchanged: the route is ADMIN-only and the
        actor is recorded.
        """
        checked_title = validate_title(title)
        checked_content = validate_content(content)

        ref = knowledge_ref_for(property_slug, topic, checked_title)

        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            existing = session.scalar(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.knowledge_ref == ref
                )
            )

            if existing is not None:
                raise ValueError(
                    "A rule with this scope, topic and title already exists."
                )

            record = HospitalityKnowledgeRecord(
                knowledge_ref=ref,
                property_slug=property_slug,
                topic=topic,
                title=checked_title,
                content=checked_content,
                status=KnowledgeStatus.APPROVED.value,
                source_type=KnowledgeSource.MANUAL.value,
                audience=audience,
                safety_status="SAFE",
                safety_reasons_json="[]",
                reason=None,
                evidence_count=0,
                evidence_property_count=0,
                evidence_refs_json="[]",
                created_at=now,
                updated_at=now,
                decided_at=now,
                decided_by_user_id=actor_user_id,
            )

            session.add(record)
            session.commit()

            return to_item(record)

    def supersede(
        self,
        knowledge_ref: str,
        actor_user_id: str,
        title: str | None = None,
        content: str | None = None,
    ) -> tuple[KnowledgeItem, KnowledgeItem]:
        """Replace an approved rule with a new one, keeping the old.

        Returns (superseded, replacement). Editing an APPROVED rule in place
        would rewrite history: a guest was told the old wording, and an audit
        trail that cannot show what the rule said last week is not much of an
        audit trail. So the old row is marked SUPERSEDED and kept, and the new
        wording becomes a new APPROVED row.
        """
        current = self.get(knowledge_ref)

        if current is None:
            raise ValueError(f"Unknown knowledge_ref: {knowledge_ref!r}")

        if current.status != KnowledgeStatus.APPROVED.value:
            raise ValueError("Only approved knowledge can be superseded.")

        new_title = validate_title(title if title is not None else current.title)
        new_content = validate_content(
            content if content is not None else current.content
        )

        if new_title == current.title and new_content == current.content:
            raise ValueError("The replacement is identical to the current rule.")

        now = datetime.now(UTC).isoformat()

        replacement_ref = knowledge_ref_for(
            current.property_slug,
            current.topic,
            new_title,
            discriminator=new_content,
        )

        with self.database.session() as session:
            old = session.scalar(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.knowledge_ref == knowledge_ref
                )
            )

            clash = session.scalar(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.knowledge_ref == replacement_ref
                )
            )

            if clash is not None:
                raise ValueError(
                    "A rule with this scope, topic and title already exists."
                )

            old.status = KnowledgeStatus.SUPERSEDED.value
            old.decided_at = now
            old.decided_by_user_id = actor_user_id
            old.updated_at = now

            record = HospitalityKnowledgeRecord(
                knowledge_ref=replacement_ref,
                property_slug=current.property_slug,
                topic=current.topic,
                title=new_title,
                content=new_content,
                status=KnowledgeStatus.APPROVED.value,
                source_type=current.source_type,
                audience=current.audience,
                safety_status=current.safety_status,
                safety_reasons_json=json.dumps(list(current.safety_reasons)),
                reason=current.reason,
                evidence_count=current.evidence_count,
                evidence_property_count=current.evidence_property_count,
                evidence_refs_json=json.dumps(list(current.evidence_refs)),
                first_observed_at=current.first_observed_at,
                last_observed_at=current.last_observed_at,
                created_at=now,
                updated_at=now,
                decided_at=now,
                decided_by_user_id=actor_user_id,
            )

            session.add(record)
            session.commit()

            return to_item(old), to_item(record)

    def update(
        self,
        knowledge_ref: str,
        actor_user_id: str,
        title: str | None = None,
        content: str | None = None,
        property_slug: str | None = None,
        scope_to_global: bool = False,
    ) -> KnowledgeItem:
        """Edit a rule's wording or scope.

        Editing does not approve. An owner who rewrites a candidate still has to
        approve it, so a careless edit cannot promote anything by itself.
        """
        now = datetime.now(UTC).isoformat()

        with self.database.session() as session:
            record = session.scalar(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.knowledge_ref == knowledge_ref
                )
            )

            if record is None:
                raise ValueError(f"Unknown knowledge_ref: {knowledge_ref!r}")

            if title is not None:
                record.title = validate_title(title)

            if content is not None:
                record.content = validate_content(content)

            # Widening scope is a deliberate act, so it needs an explicit flag
            # rather than being inferred from `property_slug=None`.
            if scope_to_global:
                record.property_slug = None

            elif property_slug is not None:
                record.property_slug = property_slug

            record.updated_at = now

            session.commit()

            return to_item(record)

    # -- reads -------------------------------------------------------------

    def get(self, knowledge_ref: str) -> KnowledgeItem | None:
        with self.database.session() as session:
            record = session.scalar(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.knowledge_ref == knowledge_ref
                )
            )

            return to_item(record) if record is not None else None

    def list_knowledge(
        self,
        status: str | None = None,
        property_slug: str | None = None,
        limit: int = 200,
    ) -> list[KnowledgeItem]:
        if status is not None and status not in {s.value for s in KnowledgeStatus}:
            raise ValueError(f"Unknown status: {status!r}")

        with self.database.session() as session:
            query = select(HospitalityKnowledgeRecord)

            if status is not None:
                query = query.where(HospitalityKnowledgeRecord.status == status)

            if property_slug is not None:
                query = query.where(
                    HospitalityKnowledgeRecord.property_slug == property_slug
                )

            query = query.order_by(HospitalityKnowledgeRecord.id.desc()).limit(limit)

            return [to_item(record) for record in session.scalars(query).all()]

    def counts(self) -> dict[str, int]:
        return {
            status.value: len(self.list_knowledge(status=status.value, limit=10_000))
            for status in KnowledgeStatus
        }

    def clear_proposed(self) -> int:
        """Discard every unreviewed candidate. Never touches a decided one.

        Re-distillation replaces the queue rather than adding to it: a PROPOSED
        row is a suggestion from one run, and a better run's suggestions should
        not sit next to a worse run's. APPROVED and REJECTED rows are decisions
        a person made and are left exactly alone -- this method cannot destroy
        owner knowledge, which is the property that matters.
        """
        with self.database.session() as session:
            records = session.scalars(
                select(HospitalityKnowledgeRecord).where(
                    HospitalityKnowledgeRecord.status == KnowledgeStatus.PROPOSED.value
                )
            ).all()

            for record in records:
                session.delete(record)

            session.commit()

            return len(records)

    def approved_for(self, property_slug: str | None) -> list[KnowledgeItem]:
        """Approved, guest-facing rules for one property: its own, plus global.

        The only read the drafting layer uses, and it is doubly filtered. A
        PROPOSED rule is invisible because nobody has reviewed it. An
        INTERNAL_OPERATION rule is invisible because it describes how the
        business runs itself -- useful to keep, wrong to say to a guest.
        """
        with self.database.session() as session:
            query = select(HospitalityKnowledgeRecord).where(
                HospitalityKnowledgeRecord.status == KnowledgeStatus.APPROVED.value
            )

            records = session.scalars(query).all()

        applicable = [
            record
            for record in records
            if (record.property_slug is None or record.property_slug == property_slug)
            and record.audience == GUEST_FACING
        ]

        # Property-specific first: a rule written for this property is the more
        # precise answer when both apply.
        applicable.sort(key=lambda record: (record.property_slug is None, record.topic))

        return [to_item(record) for record in applicable]
