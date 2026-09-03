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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.cancellation import is_cancelled
from app.connectors.lodgify.config import LODGIFY_PROPERTIES, LODGIFY_SLUGS
from app.connectors.lodgify.errors import (
    LodgifySendAmbiguous,
    LodgifySendRefused,
    LodgifyUnavailable,
)
from app.connectors.lodgify.messaging_client import (
    INBOX_STAY_FILTERS,
    STAY_FILTER_ALL,
    LodgifyMessagingClient,
)
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
    conversation_fingerprint,
    excerpt,
    message_ref_for,
    normalise_message_status,
    normalise_sender,
)
from app.connectors.lodgify.refs import conversation_ref_for, is_well_formed

MIN_LIMIT = 1

MAX_LIMIT = 100

DEFAULT_LIMIT = 20

# How many booking rows to pull per page. The provider's maximum, because every
# page costs a round trip and the whole archive has to be walked -- see
# `all_bookings` for why one page is not enough.
BOOKING_SCAN_SIZE = 100

# The only booking status that occupies the calendar. Verified live against the
# account: the vocabulary is Booked / Declined / Open, and only Booked is a stay.
OCCUPYING_STATUS = "Booked"

# A safety cap on paging, not a working limit. It was 20 while the account was
# believed to hold ~145 bookings; asking the provider for every stay rather than
# only upcoming ones revealed 1062, which is 11 pages -- so 20 was close to
# being a silent truncation. Sized well clear of the real archive, and reviewed
# whenever the account grows.
MAX_LOOKUP_PAGES = 60

# How many thread reads run at once during an Inbox scan. Modest on purpose:
# this is someone else's API, and the goal is an Inbox that returns before the
# next poll, not maximum throughput.
THREAD_READ_WORKERS = 8

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
    property_id: int | None
    property_slug: str | None
    property_name: str | None
    source: str | None
    booking_status: str | None
    arrival: str | None
    departure: str | None
    canceled_at: str | None


PROPERTIES_BY_ID = {prop.lodgify_property_id: prop for prop in LODGIFY_PROPERTIES}


def read_booking(row: dict[str, Any]) -> ResolvedConversation | None:
    """Construct the internal view of one booking, field by field.

    Nine fields are read and nothing else. The upstream row also carries a guest
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

    # Stay dates, read for the turnover question only. They are dates and
    # nothing else -- no guest block, no reservation identifier, no financials.
    arrival = row.get("arrival")
    departure = row.get("departure")

    # The authoritative cancellation signal. Lodgify spells it with one "l".
    canceled_at = row.get("canceled_at")

    return ResolvedConversation(
        conversation_ref=conversation_ref_for(booking_id),
        booking_id=booking_id,
        thread_uid=thread_uid,
        property_id=property_id if isinstance(property_id, int) else None,
        property_slug=prop.slug if prop else None,
        property_name=prop.display_name if prop else None,
        source=source if isinstance(source, str) else None,
        booking_status=status if isinstance(status, str) else None,
        arrival=arrival if isinstance(arrival, str) else None,
        departure=departure if isinstance(departure, str) else None,
        canceled_at=canceled_at if isinstance(canceled_at, str) else None,
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

    def booking_page(
        self,
        page: int,
        size: int,
        stay_filter: str = STAY_FILTER_ALL,
    ) -> list[ResolvedConversation]:
        """One page of bookings, already reduced to the fields we read."""
        return [
            resolved
            for row in self._client.list_bookings(
                size=size,
                page=page,
                stay_filter=stay_filter,
            )
            for resolved in [read_booking(row)]
            if resolved is not None
        ]

    def find_booking(
        self,
        matches,
        max_pages: int = MAX_LOOKUP_PAGES,
    ) -> ResolvedConversation | None:
        """Search the whole archive for one booking, newest first.

        Deliberately not limited to the first page. A conversation reference or
        a webhook can name any booking the account has ever had -- an old or
        declined one very much included -- and a lookup that only searched
        recent bookings would silently fail on exactly those. Verified live: the
        sandbox reservation used for webhook testing sits outside page one.

        Stops at the first match, so a recent conversation still costs one page.
        """
        for page in range(1, max_pages + 1):
            bookings = self.booking_page(page=page, size=BOOKING_SCAN_SIZE)

            if not bookings:
                return None

            for booking in bookings:
                if matches(booking):
                    return booking

            if len(bookings) < BOOKING_SCAN_SIZE:
                return None

        return None

    def find_by_thread(self, thread_uid: str) -> ResolvedConversation | None:
        """Locate the booking a thread belongs to, across the whole archive."""
        return self.find_booking(lambda booking: booking.thread_uid == thread_uid)

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

    def all_bookings(
        self,
        stay_filters: tuple[str, ...] = (STAY_FILTER_ALL,),
    ) -> list[ResolvedConversation]:
        """Every booking in the archive, deduplicated, in provider order.

        The Inbox needs all of them, not a first page. A booking carries no
        message content and no last-message timestamp -- verified live, the only
        messaging field on a booking row is `thread_uid` -- and the booking list
        is ordered by neither `created_at` nor `updated_at`. So its order says
        nothing about conversation activity, and any booking in the archive may
        be the one with today's message. Reading a subset means reading the
        wrong subset.

        Deduplicated twice over. By `conversation_ref`, because paging a live
        list is not atomic: a booking can shift between pages while the scan
        runs and be seen twice. And by `thread_uid`, because two bookings can
        share one thread -- 12 do in this account, verified live -- and the
        Inbox lists conversations, so one thread must produce one row however
        many reservations point at it.
        """
        seen: set[str] = set()
        seen_threads: set[str] = set()
        found: list[ResolvedConversation] = []

        for stay_filter in stay_filters:
            for page in range(1, MAX_LOOKUP_PAGES + 1):
                bookings = self.booking_page(
                    page=page,
                    size=BOOKING_SCAN_SIZE,
                    stay_filter=stay_filter,
                )

                if not bookings:
                    break

                for booking in bookings:
                    if booking.conversation_ref in seen:
                        continue

                    if booking.thread_uid in seen_threads:
                        continue

                    seen.add(booking.conversation_ref)
                    seen_threads.add(booking.thread_uid)
                    found.append(booking)

                if len(bookings) < BOOKING_SCAN_SIZE:
                    break

        return found

    # -- turnover ----------------------------------------------------------

    def turnover_for(self, conversation_ref: str) -> dict[str, Any]:
        """Whether another stay ends on this guest's arrival day.

        The single question the early-check-in policy turns on, and the only
        thing this returns: a date the guest already knows, and one boolean.

        `same_day_checkout` is three-valued and `None` means *we do not know*.
        That distinction is the whole point -- treating a provider failure as
        "no checkout" would turn an outage into a promise of early access on a
        turnover day. Availability cannot answer this: docs/LODGIFY_API.md
        section 4 records that a checkout day reads as *available*, because it
        is bookable for a same-day arrival. Only the stay dates can.

        Nothing about the other reservation is returned or even retained --
        not its dates, not its status, not its id, and the guest block of every
        row is never read at all.
        """
        booking = self.resolve(conversation_ref)

        arrival = booking.arrival

        if not arrival or booking.property_id is None:
            return {
                "conversation_ref": booking.conversation_ref,
                "arrival_date": arrival,
                "same_day_checkout": None,
                "reason": "The arrival date for this booking could not be read.",
            }

        try:
            occupied = self.departures_on(booking.property_id, arrival)

        except LodgifyUnavailable:
            return {
                "conversation_ref": booking.conversation_ref,
                "arrival_date": arrival,
                "same_day_checkout": None,
                "reason": "The provider could not be reached, so the schedule is unknown.",
            }

        return {
            "conversation_ref": booking.conversation_ref,
            "arrival_date": arrival,
            "same_day_checkout": any(
                other.booking_id != booking.booking_id for other in occupied
            ),
            "reason": None,
        }

    def departures_on(self, property_id: int, date: str) -> list[ResolvedConversation]:
        """Confirmed stays at one property ending on one date.

        Only `Booked` counts. A `Declined` or `Open` enquiry does not occupy the
        calendar, so counting it would deny early check-in on a day nobody is
        actually leaving.
        """
        found: list[ResolvedConversation] = []

        for page in range(1, MAX_LOOKUP_PAGES + 1):
            bookings = self.booking_page(page=page, size=BOOKING_SCAN_SIZE)

            if not bookings:
                break

            found.extend(
                booking
                for booking in bookings
                if booking.property_id == property_id
                and booking.departure == date
                and booking.booking_status == OCCUPYING_STATUS
            )

            if len(bookings) < BOOKING_SCAN_SIZE:
                break

        return found

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

        found = self.find_booking(
            lambda booking: booking.conversation_ref == conversation_ref
        )

        if found is not None:
            return found

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

        *Recently active*, not recently booked. A new message routinely arrives
        on an old reservation, so the Inbox is a view of conversation activity
        and the booking list is only how conversations are enumerated.

        The algorithm is scan -> read -> order -> limit, in that order, and the
        order is the correctness property:

          * **scan** the whole archive, because the booking list's order carries
            no activity signal (`all_bookings`);
          * **read** every candidate thread, because the last-message timestamp
            exists nowhere else;
          * **order** by that timestamp;
          * **limit** last.

        Limiting any earlier would decide what to show before knowing what is
        recent, which is exactly how a new message on an old booking went
        missing. `limit` bounds the *response*, not the work.

        Costs one booking-list call per page plus one thread read per candidate.
        There is no cheaper correct version: Lodgify exposes no thread-list
        endpoint and no last-message field on a booking, so recency cannot be
        known without reading the thread. Thread reads run concurrently to keep
        that affordable -- see `summarise_all`.
        """
        count = validate_limit(limit)

        if property_slug is not None and property_slug not in LODGIFY_SLUGS:
            raise ValueError(
                f"Unknown property: {property_slug!r}. Valid properties: "
                f"{', '.join(LODGIFY_SLUGS)}."
            )

        candidates = [
            booking
            for booking in self.all_bookings(stay_filters=INBOX_STAY_FILTERS)
            if property_slug is None or booking.property_slug == property_slug
        ]

        summaries = self.summarise_all(candidates)

        # Two stable passes rather than one composite key: the reference breaks
        # ties ascending while the timestamp sorts descending, so the order is
        # deterministic even when several threads share a timestamp. A single
        # reversed sort would reverse the tie-break too.
        summaries.sort(key=lambda summary: summary.conversation_ref)
        summaries.sort(
            key=lambda summary: summary.last_message_at or "",
            reverse=True,
        )

        return [summary.to_dict() for summary in summaries[:count]]

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

        Absence carries one meaning and one only: **we did not observe this
        conversation**. A ref that matches no booking is absent, and so is one
        whose thread could not be read -- an unknown conversation is not an
        error here, and neither is an outage. A summary that is present was
        genuinely read, even if the thread turned out to be empty.

        The fail-closed UNKNOWN placeholder `summarise` returns must never
        appear here. It is right for the live listing, where a later poll reads
        the row again, and wrong for this caller: these summaries are written
        into the activity index, and the index is the only record that a
        Historic conversation moved. Writing a placeholder's nulls over a
        webhook-recorded timestamp would sink that conversation to the bottom
        of the Inbox permanently, because the live scan can never rediscover
        it. Callers must treat absence as *keep what you already knew*.
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
            for summary in self.summarise_readable(wanted)
        }

    def summarise_readable(
        self,
        bookings: list[ResolvedConversation],
    ) -> list[ConversationSummary]:
        """Summarise only the conversations whose threads could be read.

        Same concurrency as `summarise_all`; the difference is what happens to
        a thread that will not load. Here it is dropped rather than turned into
        a placeholder, so the caller can tell an observation from an outage.
        """
        if not bookings:
            return []

        workers = min(THREAD_READ_WORKERS, len(bookings))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            attempted = list(pool.map(self.attempt_summary, bookings))

        return [summary for summary in attempted if summary is not None]

    def attempt_summary(
        self,
        booking: ResolvedConversation,
    ) -> ConversationSummary | None:
        """One inbox row, or None if the provider would not serve the thread."""
        try:
            return self.read_summary(booking)

        except LodgifyUnavailable:
            return None

    def summarise_all(
        self,
        bookings: list[ResolvedConversation],
    ) -> list[ConversationSummary]:
        """Summarise every candidate conversation, reading threads concurrently.

        Measured against the live account: 145 threads read sequentially take
        ~27s, which is longer than the console's poll interval, so requests
        would stack. The reads are independent bounded GETs and the client
        builds a fresh `httpx.Client` per call, so there is no shared state to
        protect.

        `map` preserves input order and re-raises, so behaviour is identical to
        the sequential version -- concurrency changes the latency and nothing
        else. The caller sorts regardless, so no ordering depends on completion
        order.
        """
        if not bookings:
            return []

        workers = min(THREAD_READ_WORKERS, len(bookings))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.summarise, bookings))

    def summarise(self, booking: ResolvedConversation) -> ConversationSummary:
        """One inbox row. A thread we cannot read becomes UNKNOWN, not empty.

        The fail-closed answer is deliberately indistinguishable *in its
        fields* from a genuinely empty thread, so no caller may infer failure
        from `last_message_at is None`. A caller that needs to tell the two
        apart uses `attempt_summary`, where failure is absence.
        """
        try:
            return self.read_summary(booking)

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
                fingerprint=conversation_fingerprint(()),
            )

    def read_summary(self, booking: ResolvedConversation) -> ConversationSummary:
        """One inbox row built from an actual thread read.

        Raises `LodgifyUnavailable` rather than inventing a row. Every caller
        that must distinguish a real observation from an outage goes through
        here.
        """
        messages = read_messages(self._client.get_thread(booking.thread_uid))

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
            fingerprint=conversation_fingerprint(
                [message.to_dict() for message in messages]
            ),
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
            booking_cancelled=is_cancelled(booking.booking_status, booking.canceled_at),
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
