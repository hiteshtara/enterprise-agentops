"""Historical Guest -> Owner exchanges, extracted and privacy-minimized.

The owner has years of real replies in Lodgify. They are the best available
description of how this business actually talks to guests -- far better than any
list of adjectives a prompt could carry. This module turns that archive into
short, sanitized exchange pairs.

Two rules govern everything here:

  * **Examples, never facts.** A historical reply describes *how* the owner
    answered, not what is true today. Parking that was free in March may cost
    $100 now. Nothing downstream may treat one of these rows as authoritative --
    see `app/hospitality.AUTHORITY_ORDER`.
  * **The message bodies are the only thing that survives.** No identifier, no
    contact detail, no raw payload. Sanitization happens *before* persistence,
    using the guest's own name and email from the thread to redact them by
    value, then patterns for whatever else looks like contact information or an
    access code.

Read-only with respect to Lodgify: extraction consumes thread payloads that
were already fetched, and this module never sends anything.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.database import Database, get_database
from app.db_models import HistoricalReplyExampleRecord

GUEST = "Renter"

OWNER = "Owner"

# An exchange with almost no content teaches nothing and dilutes ranking.
MIN_TEXT_LENGTH = 3

# Long enough for a real answer, short enough that one rambling thread cannot
# dominate the index.
MAX_TEXT_LENGTH = 1200

# Lodgify's own transactional mail lands in the thread as Owner messages. They
# are templates, not the owner's voice, and teaching the model to imitate them
# would be actively harmful. Matched on subject prefixes observed live.
SYSTEM_SUBJECT_MARKERS: tuple[str, ...] = (
    "your booking request for",
    "new confirmed booking",
    "payment issue for your booking",
    "your quote for",
    "booking confirmation",
    "your reservation at",
    "payment received",
    "your stay at",
    "invitation to review",
)

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Deliberately greedy: a false redaction costs one example, a missed phone
# number is a real person's number sitting in a database.
PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+")

# Door codes, lockbox codes, confirmation codes: any bare run of 4+ digits, and
# any alphanumeric token that looks like a code rather than a word.
CODE_PATTERN = re.compile(r"\b\d{4,}\b")

CONFIRMATION_PATTERN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{6,}\b")

REDACTED = "[redacted]"

# Topic tags, derived deterministically. They exist so a paraphrased question
# ("can we get in before 3?") still reaches the right precedent, which pure
# token overlap would miss. Keyword sets, not a classifier: a wrong tag costs
# ranking quality, never correctness.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "early_check_in": (
        "early check in",
        "early check-in",
        "check in early",
        "earlier check in",
        "before check in",
        "arrive early",
        "get in early",
        "get in before",
        "check in at",
    ),
    "late_checkout": (
        "late checkout",
        "late check out",
        "later checkout",
        "check out late",
        "leave later",
        "stay later",
    ),
    "checkin_process": (
        "check in",
        "check-in",
        "checkin",
        "key",
        "keys",
        "lockbox",
        "door code",
        "access",
        "self check",
        "get in",
    ),
    "checkout_process": (
        "check out",
        "checkout",
        "leaving",
        "trash",
        "keys back",
    ),
    "parking": ("parking", "park", "car", "driveway", "garage", "street parking"),
    "wifi": ("wifi", "wi-fi", "internet", "network", "password"),
    "amenities": (
        "towel",
        "linen",
        "kitchen",
        "coffee",
        "opener",
        "washer",
        "dryer",
        "iron",
        "crib",
        "tv",
        "air conditioning",
        "heating",
        "microwave",
        "dishwasher",
    ),
    "location": (
        "address",
        "directions",
        "how do we get",
        "nearest",
        "train",
        "subway",
        "airport",
        "bus",
        "walk",
        "neighborhood",
        "neighbourhood",
    ),
    "guests": ("extra guest", "another guest", "how many people", "occupancy", "pets"),
    "payment": ("payment", "invoice", "deposit", "charge", "refund", "cancel", "fee"),
    "availability": ("available", "availability", "book", "dates", "vacancy"),
    "luggage": ("luggage", "bags", "store our bags", "drop our bags"),
}


def redact(text: str, identities: tuple[str, ...] = ()) -> str:
    """Remove contact details, identifiers and codes from a message body.

    `identities` carries values we already know are personal -- the guest's name
    and email from the thread object -- so they can be removed by value rather
    than guessed at. Everything else is pattern-based.

    This runs before persistence. Nothing unsanitized is ever written.
    """
    if not isinstance(text, str):
        return ""

    # Structural patterns run FIRST, and the order is load-bearing. Substituting
    # a name first can break the pattern that would have caught the rest of the
    # value: redacting "jordan" inside "jordan@example.com" leaves
    # "[redacted]@example.com", which no longer matches the email pattern and
    # leaks the domain. Patterns take whole values; identities then mop up the
    # bare names that no pattern can see.
    cleaned = EMAIL_PATTERN.sub(REDACTED, text)
    cleaned = URL_PATTERN.sub(REDACTED, cleaned)
    cleaned = PHONE_PATTERN.sub(REDACTED, cleaned)
    cleaned = CONFIRMATION_PATTERN.sub(REDACTED, cleaned)
    cleaned = CODE_PATTERN.sub(REDACTED, cleaned)

    for identity in identities:
        if not identity or len(identity) < 3:
            continue

        # The whole value and each part of it: "Ana Silva" also redacts "Ana".
        for part in [identity, *identity.split()]:
            if len(part) < 3:
                continue

            cleaned = re.sub(
                r"(?<![A-Za-z])" + re.escape(part) + r"(?![A-Za-z])",
                REDACTED,
                cleaned,
                flags=re.IGNORECASE,
            )

    return " ".join(cleaned.split()).strip()


def is_system_message(subject: object) -> bool:
    """Whether an Owner message is Lodgify's transactional mail, not the owner."""
    if not isinstance(subject, str):
        return False

    lowered = subject.lower()

    return any(marker in lowered for marker in SYSTEM_SUBJECT_MARKERS)


def topics_for(text: str) -> list[str]:
    """Deterministic topic tags for a piece of text."""
    lowered = " ".join(text.lower().split())

    return sorted(
        topic
        for topic, keywords in TOPIC_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    )


def fingerprint(
    property_slug: str | None,
    guest_text: str,
    owner_text: str,
    created_at: str | None,
) -> str:
    """A stable identity for one exchange.

    Built from sanitized content plus the day it happened, so re-running the
    index finds the existing row instead of inserting a duplicate. The day
    rather than the timestamp: the same exchange re-read later must hash the
    same, and a second identical exchange on a different day is genuinely a
    different precedent.
    """
    day = (created_at or "")[:10]

    material = "\x1f".join([property_slug or "", guest_text, owner_text, day])

    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]


@dataclass(frozen=True)
class HistoricalExchange:
    """One sanitized Guest -> Owner pair, ready to persist."""

    example_ref: str
    property_slug: str | None
    source: str | None
    guest_text: str
    owner_text: str
    topics: tuple[str, ...]
    created_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_ref": self.example_ref,
            "property_slug": self.property_slug,
            "source": self.source,
            "guest_text": self.guest_text,
            "owner_text": self.owner_text,
            "topics": list(self.topics),
            "created_at": self.created_at,
        }


def extract_exchanges(
    messages: list[dict[str, Any]],
    property_slug: str | None = None,
    source: str | None = None,
    identities: tuple[str, ...] = (),
) -> list[HistoricalExchange]:
    """Turn one chronological thread into Guest -> Owner exchange pairs.

    The algorithm, walked once in order:

      1. Collect a contiguous run of Renter messages. Guests often send three
         short messages where one would do; they are joined into a single
         question so the owner's reply is paired with all of it.
      2. Collect the contiguous run of Owner messages that follows. Owners
         likewise reply in pieces; those are joined into one answer.
      3. Emit one exchange, then continue from the next Renter run.

    A guest run with no owner run after it produces nothing -- that is an
    unanswered question, not a precedent. Owner messages before any guest
    message are skipped: there is no question they answer.

    Sanitization is applied to each side before the pair is built, and an
    exchange whose either side is empty afterwards is dropped.
    """
    rows = [row for row in (messages or []) if isinstance(row, dict)]

    exchanges: list[HistoricalExchange] = []
    seen: set[str] = set()

    index = 0

    while index < len(rows):
        # Skip anything before the first guest turn.
        if rows[index].get("sender") != GUEST:
            index += 1
            continue

        guest_parts: list[str] = []
        guest_created: str | None = None

        while index < len(rows) and rows[index].get("sender") == GUEST:
            body = redact(rows[index].get("message") or "", identities)

            if body:
                guest_parts.append(body)

                if guest_created is None:
                    guest_created = rows[index].get("created_at")

            index += 1

        owner_parts: list[str] = []

        while index < len(rows) and rows[index].get("sender") == OWNER:
            row = rows[index]

            # Lodgify's own transactional mail is not the owner's voice.
            if not is_system_message(row.get("subject")):
                body = redact(row.get("message") or "", identities)

                if body:
                    owner_parts.append(body)

            index += 1

        guest_text = " ".join(guest_parts).strip()[:MAX_TEXT_LENGTH]
        owner_text = " ".join(owner_parts).strip()[:MAX_TEXT_LENGTH]

        if len(guest_text) < MIN_TEXT_LENGTH or len(owner_text) < MIN_TEXT_LENGTH:
            continue

        ref = fingerprint(property_slug, guest_text, owner_text, guest_created)

        if ref in seen:
            continue

        seen.add(ref)

        exchanges.append(
            HistoricalExchange(
                example_ref=ref,
                property_slug=property_slug,
                source=source,
                guest_text=guest_text,
                owner_text=owner_text,
                topics=tuple(topics_for(guest_text)),
                created_at=guest_created,
            )
        )

    return exchanges


@dataclass
class IndexReport:
    """Counts only. Never carries a message body."""

    bookings_scanned: int = 0
    threads_read: int = 0
    thread_errors: int = 0
    exchanges_extracted: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "bookings_scanned": self.bookings_scanned,
            "threads_read": self.threads_read,
            "thread_errors": self.thread_errors,
            "exchanges_extracted": self.exchanges_extracted,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
        }


class HistoricalReplyStore:
    """Persistence for historical examples. Opens and closes its own session."""

    def __init__(self, database: Database | None = None) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database or get_database()

    def upsert(self, exchanges: list[HistoricalExchange]) -> tuple[int, int]:
        """Insert new exchanges, refresh ones already present.

        Returns (created, updated). Running the same index twice creates
        nothing the second time -- the fingerprint is the identity.
        """
        created = 0
        updated = 0

        with self.database.session() as session:
            for exchange in exchanges:
                existing = session.scalar(
                    select(HistoricalReplyExampleRecord).where(
                        HistoricalReplyExampleRecord.example_ref == exchange.example_ref
                    )
                )

                if existing is None:
                    session.add(
                        HistoricalReplyExampleRecord(
                            example_ref=exchange.example_ref,
                            property_slug=exchange.property_slug,
                            source=exchange.source,
                            guest_text=exchange.guest_text,
                            owner_text=exchange.owner_text,
                            topics_json=json.dumps(list(exchange.topics)),
                            created_at=exchange.created_at,
                            indexed_at=datetime.now(UTC).isoformat(),
                        )
                    )

                    created += 1

                else:
                    existing.property_slug = exchange.property_slug
                    existing.source = exchange.source
                    existing.topics_json = json.dumps(list(exchange.topics))
                    existing.indexed_at = datetime.now(UTC).isoformat()

                    updated += 1

            session.commit()

        return created, updated

    def all_examples(self) -> list[dict[str, Any]]:
        """Every stored example, as plain dicts."""
        with self.database.session() as session:
            rows = session.scalars(select(HistoricalReplyExampleRecord)).all()

            return [
                {
                    "example_ref": row.example_ref,
                    "property_slug": row.property_slug,
                    "source": row.source,
                    "guest_text": row.guest_text,
                    "owner_text": row.owner_text,
                    "topics": row.topics,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def count(self) -> int:
        with self.database.session() as session:
            return len(session.scalars(select(HistoricalReplyExampleRecord.id)).all())


def index_one_conversation(
    inbox: Any,
    store: "HistoricalReplyStore",
    conversation_ref: str,
) -> tuple[int, int]:
    """Fold one conversation into the historical index. Returns (created, updated).

    The targeted learning path: after a reply is *confirmed* sent, that
    exchange is now a real example of how this owner answers, so it becomes
    available as precedent. One thread read, no scheduler, no periodic scan.

    Deliberately narrow about when it runs. A reply that failed, or whose
    delivery is unknown, is not an example of anything -- and indexing an
    unconfirmed send would teach the model from a message that may never have
    arrived.

    It stops at the historical index. Nothing here distils knowledge, approves
    anything, or sends: an example becomes a *proposed* rule only through the
    explicit distillation command and a human review.
    """
    booking = inbox.find_booking(
        lambda candidate: candidate.conversation_ref == conversation_ref
    )

    if booking is None:
        return 0, 0

    thread = inbox.thread_for_indexing(booking.thread_uid)

    exchanges = extract_exchanges(
        [message.to_dict() for message in thread.messages],
        property_slug=booking.property_slug,
        source=booking.source,
        # The guest's own name and email, removed from message bodies by value.
        # Neither is persisted.
        identities=thread.identities,
    )

    # The same fingerprint the full rebuild uses, so a conversation indexed here
    # and later re-indexed by the full command produces no duplicates.
    return store.upsert(exchanges)
