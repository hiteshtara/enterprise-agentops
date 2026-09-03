"""The guest-conversation inbox: discovery, sanitization, and the governed send.

This is where provider identifiers stop. Above this line everything speaks
`conversation_ref`; below it, and only inside this module, live `booking_id` and
`thread_uid`.

The send path is the reason this module is written the way it is. Lodgify's send
endpoint answers 200 with an empty body and offers no idempotency key, so:

  * there is no identifier in the response to correlate against, and
  * a retry can deliver a second real message to a real guest.

So the algorithm is snapshot -> send exactly once -> re-read -> diff, and every
uncertain outcome resolves to UNKNOWN_SEND_STATE rather than to a guess. See
docs/LODGIFY_API.md sections 12, 14, 16 and 17.
"""

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.connectors.lodgify.config import LODGIFY_PROPERTIES, LODGIFY_SLUGS
from app.connectors.lodgify.errors import (
    LodgifySendAmbiguous,
    LodgifySendRefused,
    LodgifyUnavailable,
)
from app.connectors.lodgify.messaging_client import LodgifyMessagingClient
from app.connectors.lodgify.messaging_models import (
    SENDER_OWNER,
    SENDER_RENTER,
    Conversation,
    ConversationMessage,
    ConversationStatus,
    ConversationSummary,
    SendOutcome,
    SendStatus,
    SentMessage,
    excerpt,
    message_ref_for,
    normalise_message_status,
    normalise_sender,
)
from app.connectors.lodgify.refs import conversation_ref_for, is_well_formed

MIN_LIMIT = 1

MAX_LIMIT = 100

DEFAULT_LIMIT = 20

# How many booking rows to pull before filtering. One page is enough for an
# inbox view; a property filter can thin the page out, so it is deliberately
# larger than the default limit.
BOOKING_SCAN_SIZE = 100

MAX_SUBJECT_LENGTH = 200

MAX_MESSAGE_LENGTH = 4000

# A single send fans out to one row per configured route on the thread. Two is
# the most observed live (SMS + channel). Anything past this is not fan-out any
# more, and correlation is no longer trustworthy.
MAX_FANOUT_ROWS = 5

# Fan-out rows from one send share a timestamp. Rows further apart than this
# were probably not all ours, so attribution is unsafe.
CORRELATION_WINDOW_SECONDS = 120

TAG_PATTERN = re.compile(r"<[^>]+>")

# Everything in C0 except tab and newline, plus DEL. `\r` is included
# deliberately: browsers hand us LF-only textarea values, so a CR means the text
# came from somewhere we have not accounted for.
FORBIDDEN_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

HTML_TAG_PATTERN = re.compile(r"<\s*/?\s*[A-Za-z]")

UNKNOWN_SEND_MESSAGE = (
    "Delivery could not be confirmed. Do not resend automatically. Check the "
    "Lodgify thread before taking further action."
)


def plain_text(value: object) -> str:
    """Render a provider message body as plain text.

    Owner messages composed in the Lodgify dashboard contain HTML; guest
    messages generally do not. Tags are stripped and entities decoded so the
    model reasons about, and the console renders, the same plain text a person
    would read.

    This normalises *inbound* text only. Outbound text is never rewritten -- see
    `validate_message`.
    """
    if not isinstance(value, str):
        return ""

    return html.unescape(TAG_PATTERN.sub(" ", value)).strip()


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        # Python 3.11+ parses a trailing "Z" directly.
        return datetime.fromisoformat(value)

    except ValueError:
        return None


def validate_subject(value: object) -> str:
    """Check an outbound subject. Rejects; never rewrites."""
    if not isinstance(value, str):
        raise TypeError(f"subject must be a string, got {type(value).__name__}.")

    if not value.strip():
        raise ValueError("subject must not be empty.")

    if len(value) > MAX_SUBJECT_LENGTH:
        raise ValueError(
            f"subject must be {MAX_SUBJECT_LENGTH} characters or fewer, "
            f"got {len(value)}."
        )

    if FORBIDDEN_CONTROL_PATTERN.search(value) or "\n" in value:
        raise ValueError("subject must be a single line of plain text.")

    if HTML_TAG_PATTERN.search(value):
        raise ValueError("subject must be plain text; HTML is not supported.")

    return value


def validate_message(value: object) -> str:
    """Check an outbound message body. Rejects; never rewrites.

    Returning the value unchanged is the point: whatever a human approved is
    what gets transmitted, byte for byte. A validator that quietly repaired its
    input would break the guarantee that the text on the approval card is the
    text the guest receives.
    """
    if not isinstance(value, str):
        raise TypeError(f"message must be a string, got {type(value).__name__}.")

    if not value.strip():
        raise ValueError("message must not be empty.")

    if len(value) > MAX_MESSAGE_LENGTH:
        raise ValueError(
            f"message must be {MAX_MESSAGE_LENGTH} characters or fewer, "
            f"got {len(value)}."
        )

    if FORBIDDEN_CONTROL_PATTERN.search(value):
        raise ValueError(
            "message must be plain text without control characters. Use "
            "newlines to separate paragraphs."
        )

    if HTML_TAG_PATTERN.search(value):
        raise ValueError("message must be plain text; HTML is not supported.")

    return value


def validate_limit(value: object) -> int:
    if value is None:
        return DEFAULT_LIMIT

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"limit must be an integer, got {type(value).__name__}.")

    if value < MIN_LIMIT or value > MAX_LIMIT:
        raise ValueError(
            f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}, got {value}."
        )

    return value


@dataclass(frozen=True)
class ThreadForIndexing:
    """One thread reduced to what the history indexer needs.

    `identities` exists to be removed from the message bodies, never to be
    stored. See `LodgifyInbox.thread_for_indexing`.
    """

    messages: tuple["ConversationMessage", ...]
    identities: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedConversation:
    """A conversation_ref resolved to what the provider needs.

    Never returned to a caller. `booking_id` and `thread_uid` exist on this
    object and nowhere above it.
    """

    conversation_ref: str
    booking_id: int
    thread_uid: str
    property_slug: str | None
    property_name: str | None
    source: str | None
    booking_status: str | None


PROPERTIES_BY_ID = {prop.lodgify_property_id: prop for prop in LODGIFY_PROPERTIES}


def read_booking(row: dict[str, Any]) -> ResolvedConversation | None:
    """Construct the internal view of one booking, field by field.

    Five fields are read and nothing else. The upstream row also carries a guest
    block with name, email and phone, the originating IP address, financial
    totals, transactions, notes, and `source_text` -- which
    docs/LODGIFY_API.md section 6 records as untrusted free text that has been
    observed holding an embedded JSON blob. None of it is read, so none of it
    can leak.
    """
    booking_id = row.get("id")
    thread_uid = row.get("thread_uid")

    if not isinstance(booking_id, int) or isinstance(booking_id, bool):
        return None

    if not isinstance(thread_uid, str) or not thread_uid:
        return None

    property_id = row.get("property_id")
    prop = PROPERTIES_BY_ID.get(property_id) if isinstance(property_id, int) else None

    source = row.get("source")
    status = row.get("status")

    return ResolvedConversation(
        conversation_ref=conversation_ref_for(booking_id),
        booking_id=booking_id,
        thread_uid=thread_uid,
        property_slug=prop.slug if prop else None,
        property_name=prop.display_name if prop else None,
        source=source if isinstance(source, str) else None,
        booking_status=status if isinstance(status, str) else None,
    )


def read_message(row: dict[str, Any]) -> ConversationMessage | None:
    """Construct one sanitized message, field by field.

    `route` is read nowhere. A live send recorded `route: null` while being
    genuinely delivered, so the field cannot support a delivery claim and is not
    worth carrying -- docs/LODGIFY_API.md section 12.
    """
    identifier = row.get("message_id") or row.get("id")

    if identifier is None:
        return None

    body = plain_text(row.get("message"))
    subject = row.get("subject")

    return ConversationMessage(
        message_ref=message_ref_for(identifier),
        sender=normalise_sender(row.get("type")),
        subject=plain_text(subject) if isinstance(subject, str) else None,
        message=body,
        created_at=row.get("date_created")
        if isinstance(row.get("date_created"), str)
        else None,
        message_status=normalise_message_status(row.get("message_status")),
    )


def read_messages(thread: dict[str, Any]) -> tuple[ConversationMessage, ...]:
    """Every message in a thread, oldest first.

    Upstream returns `messages` **newest-first**. AgentGuard normalises to
    chronological order once, here, because a model reasons about a conversation
    the way a person reads one and a console that rendered newest-first would
    invert every thread. Ordering is by `date_created` rather than by reversing
    the array, so a thread that arrives in a different order still comes out
    right. See docs/LODGIFY_API.md section 7.
    """
    rows = thread.get("messages")

    if not isinstance(rows, list):
        return ()

    messages = [
        message
        for row in rows
        if isinstance(row, dict)
        for message in [read_message(row)]
        if message is not None
    ]

    return tuple(
        sorted(
            messages,
            key=lambda message: (message.created_at or "", message.message_ref),
        )
    )


def classify_conversation(
    messages: tuple[ConversationMessage, ...],
) -> ConversationStatus:
    """Whether a conversation appears to be waiting on us.

    The V1 rule, deliberately built only from data the provider actually proves:

      * the newest message is from the guest (`Renter`)  -> NEEDS_ATTENTION
      * the newest message is from us (`Owner`)          -> RESPONDED
      * no messages, or an unrecognised sender type      -> UNKNOWN

    Nothing else is used. The booking object carries no proven replied/read
    signal, and the thread's own `is_read` reflects whether someone opened the
    thread in Lodgify -- not whether the guest got an answer -- so neither is
    allowed to influence this.

    Uncertainty resolves to UNKNOWN, never to NEEDS_ATTENTION. Over-reporting
    would train the operator to ignore the flag, which costs more than the
    occasional missed row.
    """
    if not messages:
        return ConversationStatus.UNKNOWN

    sender = messages[-1].sender

    if sender == SENDER_RENTER:
        return ConversationStatus.NEEDS_ATTENTION

    if sender == SENDER_OWNER:
        return ConversationStatus.RESPONDED

    return ConversationStatus.UNKNOWN


def summarise_delivery(messages: tuple[SentMessage, ...]) -> str:
    """Report what the provider says about delivery, and nothing more.

    Never names a channel. Dashboard-originated messages on OTA threads show
    `route` values of Vrbo/Airbnb/BookingCom, but API-originated routing has not
    been verified, so "sent to Airbnb" is a claim the data does not support --
    docs/LODGIFY_API.md section 19.
    """
    statuses = {message.message_status for message in messages}

    # Weakest true claim wins when rows disagree: a failure is the thing the
    # operator must act on, and "Sent" must never be upgraded to "Delivered"
    # just because a sibling row reported delivery.
    if "Failed" in statuses:
        return "Lodgify reports the message as Failed."

    if "Sent" in statuses:
        return "Lodgify reports the message as Sent."

    if "Delivered" in statuses:
        return "Lodgify reports the message as Delivered."

    return "Lodgify accepted the message. It does not report a delivery status."


class LodgifyInbox:
    """Guest conversations, sanitized, plus the one governed write."""

    def __init__(self, client: LodgifyMessagingClient) -> None:
        self._client = client

    # -- archive access ----------------------------------------------------
    #
    # Public seams for the history indexer, which walks the whole archive
    # rather than the first page. They expose the same sanitization the Inbox
    # uses; nothing here hands out a raw payload.

    @property
    def client(self) -> LodgifyMessagingClient:
        return self._client

    def booking_page(self, page: int, size: int) -> list[ResolvedConversation]:
        """One page of bookings, already reduced to the five fields we read."""
        return [
            resolved
            for row in self._client.list_bookings(size=size, page=page)
            for resolved in [read_booking(row)]
            if resolved is not None
        ]

    def thread_for_indexing(self, thread_uid: str) -> "ThreadForIndexing":
        """Messages plus the guest identity strings, for redaction only.

        This is the single place `guest_name` and `guest_email` leave the raw
        payload, and they leave it for one purpose: so the indexer can remove
        those exact values from message bodies before anything is stored.
        Redacting by known value beats pattern-matching a name. Neither string
        is persisted, returned to a caller, or logged.
        """
        thread = self._client.get_thread(thread_uid)

        identities = tuple(
            value.strip()
            for key in ("guest_name", "guest_email")
            for value in [thread.get(key)]
            if isinstance(value, str) and value.strip()
        )

        return ThreadForIndexing(
            messages=read_messages(thread),
            identities=identities,
        )

    # -- resolution --------------------------------------------------------

    def bookings(self) -> list[ResolvedConversation]:
        rows = self._client.list_bookings(size=BOOKING_SCAN_SIZE)

        return [
            resolved
            for row in rows
            for resolved in [read_booking(row)]
            if resolved is not None
        ]

    def resolve(self, conversation_ref: object) -> ResolvedConversation:
        """Turn a safe ref into provider identifiers, or refuse.

        Resolution matches against bookings this account actually has. A
        fabricated or stale ref matches nothing and raises a ValueError, which
        the agent loop treats as recoverable -- so a model that invents a ref is
        told so and can correct itself, and never reaches a reservation it was
        not shown.
        """
        if not is_well_formed(conversation_ref):
            raise ValueError(
                f"Unknown conversation_ref: {conversation_ref!r}. Use a "
                f"conversation_ref returned by list_recent_guest_conversations."
            )

        for booking in self.bookings():
            if booking.conversation_ref == conversation_ref:
                return booking

        raise ValueError(
            f"Unknown conversation_ref: {conversation_ref!r}. It does not match "
            f"any recent conversation."
        )

    # -- reads -------------------------------------------------------------

    def list_conversations(
        self,
        property_slug: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recent conversations, most recently active first.

        Costs one booking-list call plus one thread read per row returned. The
        thread read is unavoidable: the booking object carries no message
        content, no last-message timestamp and no proven reply state, so the
        only way to say anything true about a conversation is to read it.
        `limit` is what bounds that cost.
        """
        count = validate_limit(limit)

        if property_slug is not None and property_slug not in LODGIFY_SLUGS:
            raise ValueError(
                f"Unknown property: {property_slug!r}. Valid properties: "
                f"{', '.join(LODGIFY_SLUGS)}."
            )

        candidates = [
            booking
            for booking in self.bookings()
            if property_slug is None or booking.property_slug == property_slug
        ][:count]

        summaries = [self.summarise(booking) for booking in candidates]

        return [
            summary.to_dict()
            for summary in sorted(
                summaries,
                key=lambda summary: summary.last_message_at or "",
                reverse=True,
            )
        ]

    def summarise(self, booking: ResolvedConversation) -> ConversationSummary:
        """One inbox row. A thread we cannot read becomes UNKNOWN, not empty."""
        try:
            messages = read_messages(self._client.get_thread(booking.thread_uid))

        except LodgifyUnavailable:
            # Fail closed: an unreadable thread is not "responded" and not
            # "needs attention". It is unknown, and says so.
            return ConversationSummary(
                conversation_ref=booking.conversation_ref,
                property_slug=booking.property_slug,
                property_name=booking.property_name,
                source=booking.source,
                booking_status=booking.booking_status,
                status=ConversationStatus.UNKNOWN,
                last_message_at=None,
                last_message_sender=None,
                last_message_excerpt=None,
                message_count=0,
            )

        latest = messages[-1] if messages else None

        return ConversationSummary(
            conversation_ref=booking.conversation_ref,
            property_slug=booking.property_slug,
            property_name=booking.property_name,
            source=booking.source,
            booking_status=booking.booking_status,
            status=classify_conversation(messages),
            last_message_at=latest.created_at if latest else None,
            last_message_sender=latest.sender if latest else None,
            last_message_excerpt=excerpt(latest.message) if latest else None,
            message_count=len(messages),
        )

    def get_conversation(self, conversation_ref: str) -> dict[str, Any]:
        booking = self.resolve(conversation_ref)

        thread = self._client.get_thread(booking.thread_uid)
        messages = read_messages(thread)

        is_read = thread.get("is_read")

        return Conversation(
            conversation_ref=booking.conversation_ref,
            property_slug=booking.property_slug,
            property_name=booking.property_name,
            source=booking.source,
            booking_status=booking.booking_status,
            subject=plain_text(thread.get("subject")) or None,
            is_read=is_read if isinstance(is_read, bool) else None,
            status=classify_conversation(messages),
            messages=messages,
        ).to_dict()

    # -- the one write -----------------------------------------------------

    def send_reply(
        self,
        conversation_ref: str,
        subject: str,
        message: str,
    ) -> dict[str, Any]:
        """Send one guest reply and verify it by re-reading the thread.

        Arguments are validated before anything leaves the process, so a bad
        argument costs nothing and is recoverable. After the POST, every
        uncertain path resolves to UNKNOWN_SEND_STATE -- which is not a failure,
        is never retried, and asks for a human.
        """
        checked_subject = validate_subject(subject)
        checked_message = validate_message(message)

        booking = self.resolve(conversation_ref)

        try:
            before = read_messages(self._client.get_thread(booking.thread_uid))

        except LodgifyUnavailable:
            # No snapshot means no way to verify afterwards. Refuse before
            # sending rather than send something we could never confirm.
            return SendOutcome(
                conversation_ref=booking.conversation_ref,
                status=SendStatus.CONFIRMED_FAILED,
                message=(
                    "The conversation could not be read before sending, so "
                    "nothing was sent."
                ),
            ).to_dict()

        known_refs = {existing.message_ref for existing in before}

        try:
            # Exactly one POST. There is no retry here and none may be added.
            self._client.post_message(
                booking_id=booking.booking_id,
                subject=checked_subject,
                message=checked_message,
            )

        except LodgifySendRefused as exc:
            return SendOutcome(
                conversation_ref=booking.conversation_ref,
                status=SendStatus.CONFIRMED_FAILED,
                message=f"Nothing was sent. {exc}",
            ).to_dict()

        except LodgifySendAmbiguous:
            return SendOutcome(
                conversation_ref=booking.conversation_ref,
                status=SendStatus.UNKNOWN_SEND_STATE,
                message=UNKNOWN_SEND_MESSAGE,
            ).to_dict()

        return self.verify(booking, checked_subject, checked_message, known_refs)

    def verify(
        self,
        booking: ResolvedConversation,
        subject: str,
        message: str,
        known_refs: set[str],
    ) -> dict[str, Any]:
        """Find the rows our send created, or admit that we cannot.

        The POST returns no identifier, so attribution is by difference: rows
        that were not there before, from us, carrying exactly the text we sent.
        """
        try:
            after = read_messages(self._client.get_thread(booking.thread_uid))

        except LodgifyUnavailable:
            # The message may well have been sent -- we just cannot show it.
            return SendOutcome(
                conversation_ref=booking.conversation_ref,
                status=SendStatus.UNKNOWN_SEND_STATE,
                message=UNKNOWN_SEND_MESSAGE,
            ).to_dict()

        matches = [
            row
            for row in after
            if row.message_ref not in known_refs
            and row.sender == SENDER_OWNER
            and row.subject == subject
            and row.message == message
        ]

        if not matches or not self.correlated(matches):
            return SendOutcome(
                conversation_ref=booking.conversation_ref,
                status=SendStatus.UNKNOWN_SEND_STATE,
                message=UNKNOWN_SEND_MESSAGE,
            ).to_dict()

        created = tuple(
            SentMessage(
                message_ref=row.message_ref,
                message_status=row.message_status,
                created_at=row.created_at,
            )
            for row in matches
        )

        return SendOutcome(
            conversation_ref=booking.conversation_ref,
            status=SendStatus.CONFIRMED_SENT,
            message=summarise_delivery(created),
            messages=created,
        ).to_dict()

    def correlated(self, matches: list[ConversationMessage]) -> bool:
        """Whether the matching rows can safely be attributed to one send.

        Snapshot-then-diff is inherently racy: another actor can write an
        identical message into the same thread inside the window, and rows that
        look like ours would then not be. Two conservative guards:

          * more rows than fan-out plausibly explains, and
          * rows spread wider in time than one send produces.

        Either one means we stop guessing and report UNKNOWN_SEND_STATE.
        """
        if len(matches) == 1:
            return True

        if len(matches) > MAX_FANOUT_ROWS:
            return False

        timestamps = [parse_timestamp(row.created_at) for row in matches]

        if any(stamp is None for stamp in timestamps):
            # Several rows and no way to check they belong together.
            return False

        ordered = sorted(stamp for stamp in timestamps if stamp is not None)
        spread = (ordered[-1] - ordered[0]).total_seconds()

        return abs(spread) <= CORRELATION_WINDOW_SECONDS
