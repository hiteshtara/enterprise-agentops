"""Open enquiries: listing, resolution, thread reads, and the one governed send.

Deliberately separate from `app/connectors/lodgify/inbox.py`. That module is
the booked-guest pipeline -- discovery, activity, drafts, and its own governed
send. This one is the operator's enquiry surface.

Two classes, and the split between them is the safety property:

  * `LodgifyEnquiries` reads. It has no send method at all, and it is the only
    object the drafting service is given, so there is no code path from
    drafting to a provider write.
  * `LodgifyEnquirySender` writes, once, to the documented enquiry endpoint,
    and is reachable only through the DANGEROUS `send_enquiry_reply` tool
    behind a recorded human approval. It never touches the booking send
    endpoint: an enquiry is not a booking, and there is no fallback between
    them.

Provider facts this module is built on, verified live against the account on
2026-09-03 and not to be re-derived:

  * `GET /v1/reservation` is the enquiry-bearing list. Paging is **offset and
    limit**. `page`/`size` are silently ignored, which would mean every page
    after the first was a duplicate of the first -- so those spellings must
    never appear here.
  * Rows carry `type` of `Booking`, `Enquiry` or `ClosedPeriod`. `ClosedPeriod`
    rows are calendar blocks, not conversations.
  * **The `type=Enquiry` query filter is ignored by the provider.** Filtering
    happens here, in our code, on the row's own `type`. Nothing may rely on the
    provider having narrowed the list.
  * `GET /v1/reservation/enquiry/{id}` answers 500 for every enquiry tried. It
    is not used, and must not be.
  * The thread is read with `GET /v2/messaging/{threadGuid}`, the same endpoint
    and the same client the booking path uses.
  * A message is added with `POST /v1/reservation/enquiry/{id}/messages`, the
    documented enquiry sibling of the booking send (docs/LODGIFY_API.md section
    10). `POST /v1/reservation/booking/{id}/messages` is never used here, in
    any circumstance, including as a fallback.

Provider identifiers stop here, exactly as they do in the inbox module. Above
this line everything speaks `enquiry_ref`; the numeric id and the `thread_uid`
exist on `ResolvedEnquiry` and nowhere above it.
"""

from dataclasses import dataclass
from typing import Any

from app.connectors.lodgify.config import LODGIFY_PROPERTIES
from app.connectors.lodgify.errors import (
    LodgifySendAmbiguous,
    LodgifySendRefused,
    LodgifyUnavailable,
)
from app.connectors.lodgify.inbox import (
    UNKNOWN_SEND_MESSAGE,
    correlated,
    read_messages,
    summarise_delivery,
    validate_message,
    validate_subject,
)
from app.connectors.lodgify.messaging_client import LodgifyMessagingClient
from app.connectors.lodgify.messaging_models import (
    SENDER_OWNER,
    ConversationMessage,
    SendOutcome,
    SendStatus,
    SentMessage,
)
from app.connectors.lodgify.refs import enquiry_ref_for, is_well_formed_enquiry_ref

RESERVATION_PATH = "/v1/reservation"

# The narrowing that makes this affordable: `status=Open` takes the list from
# the whole archive to ~117 rows, about three pages.
OPEN_STATUS = "Open"

ENQUIRY_TYPE = "Enquiry"

# Rows per request. The provider honours `limit=50`.
PAGE_SIZE = 50

# A safety cap on paging, not a working limit. `status=Open` is ~117 rows
# against the live account, so 12 pages is six times the observed need and
# still bounds a provider that starts answering `next` forever.
MAX_PAGES = 12

MIN_LIMIT = 1

MAX_LIMIT = 100

DEFAULT_LIMIT = 20

UNKNOWN_ENQUIRY = "Unknown enquiry."

PROPERTIES_BY_ID = {prop.lodgify_property_id: prop for prop in LODGIFY_PROPERTIES}


@dataclass(frozen=True)
class ResolvedEnquiry:
    """One open enquiry, resolved to what the provider needs.

    Never returned to a caller. `enquiry_id` and `thread_uid` exist on this
    object and nowhere above it; `summary()` is the only projection that
    crosses the API boundary.
    """

    enquiry_ref: str
    enquiry_id: int
    thread_uid: str
    property_slug: str | None
    property_name: str | None
    source: str | None
    arrival: str | None
    departure: str | None
    is_replied: bool | None
    # Read for ordering only, and deliberately absent from `summary()`. The
    # operator needs the newest enquiry first; they do not need a timestamp
    # they cannot act on.
    created_at: str | None

    def summary(self) -> dict[str, Any]:
        """The safe metadata, and only that.

        No guest name, email or phone. No numeric id, no `thread_uid`, no
        `upgraded_enquiry_id`, no totals, and no `source_text` -- which
        docs/LODGIFY_API.md section 6 records as untrusted free text observed
        carrying an embedded JSON blob.
        """
        return {
            "enquiry_ref": self.enquiry_ref,
            "property_slug": self.property_slug,
            "property_name": self.property_name,
            "source": self.source,
            "arrival": self.arrival,
            "departure": self.departure,
            "is_replied": self.is_replied,
        }


def read_enquiry(row: dict[str, Any]) -> ResolvedEnquiry | None:
    """Construct the internal view of one enquiry, field by field.

    Eight fields are read and nothing else. The upstream row also carries a
    guest block with name, email and phone, `source_text`, totals, and
    `upgraded_enquiry_id`; none of it is read, so none of it can leak.

    A row without a usable id or thread is dropped rather than guessed at: an
    enquiry whose thread cannot be addressed is one this feature cannot do
    anything with.
    """
    enquiry_id = row.get("id")
    thread_uid = row.get("thread_uid")

    if not isinstance(enquiry_id, int) or isinstance(enquiry_id, bool):
        return None

    if not isinstance(thread_uid, str) or not thread_uid:
        return None

    property_id = row.get("property_id")
    prop = PROPERTIES_BY_ID.get(property_id) if isinstance(property_id, int) else None

    source = row.get("source")
    arrival = row.get("arrival")
    departure = row.get("departure")
    is_replied = row.get("is_replied")
    created_at = row.get("created_at")

    return ResolvedEnquiry(
        enquiry_ref=enquiry_ref_for(enquiry_id),
        enquiry_id=enquiry_id,
        thread_uid=thread_uid,
        # The property name comes from our own closed allowlist, never from the
        # provider's row, for the same reason the booking path does it: the
        # console must show a name this deployment actually knows about.
        property_slug=prop.slug if prop else None,
        property_name=prop.display_name if prop else None,
        source=source if isinstance(source, str) else None,
        arrival=arrival if isinstance(arrival, str) else None,
        departure=departure if isinstance(departure, str) else None,
        is_replied=is_replied if isinstance(is_replied, bool) else None,
        created_at=created_at if isinstance(created_at, str) else None,
    )


def validate_limit(value: object) -> int:
    """Bound the response size. Raises rather than coercing a bad value."""
    if value is None:
        return DEFAULT_LIMIT

    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"limit must be an integer, got {type(value).__name__}.")

    if value < MIN_LIMIT or value > MAX_LIMIT:
        raise ValueError(
            f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}, got {value}."
        )

    return value


class LodgifyEnquiries:
    """Reads open enquiries and their threads. It has no write method."""

    def __init__(self, client: LodgifyMessagingClient) -> None:
        self._client = client

    def pages(self) -> list[dict[str, Any]]:
        """Every open reservation row, walked by offset.

        `offset`/`limit` are the only paging parameters sent. Sending `page` or
        `size` here would be worse than useless: the provider ignores them, so
        every request would return the first page again and the walk would
        either loop or silently truncate.
        """
        rows: list[dict[str, Any]] = []

        for page in range(MAX_PAGES):
            payload = self._client.get(
                RESERVATION_PATH,
                {
                    "status": OPEN_STATUS,
                    "offset": str(page * PAGE_SIZE),
                    "limit": str(PAGE_SIZE),
                },
            )

            if not isinstance(payload, dict):
                raise LodgifyUnavailable(
                    "The provider returned enquiries in an unexpected shape."
                )

            items = payload.get("items")

            if not isinstance(items, list):
                raise LodgifyUnavailable(
                    "The provider returned enquiries in an unexpected shape."
                )

            rows.extend(row for row in items if isinstance(row, dict))

            # Two independent stop conditions, because either one alone has a
            # failure mode: a short page means the end regardless of what
            # `next` says, and an absent `next` means the end even when the
            # last page happened to be full.
            if len(items) < PAGE_SIZE or not payload.get("next"):
                break

        return rows

    def scan(self) -> list[ResolvedEnquiry]:
        """Open enquiries, newest first.

        The `type` test is the whole filter and it happens here, on the row we
        actually received -- `Booking` and `ClosedPeriod` are excluded by not
        matching, rather than by a provider parameter that is ignored.
        """
        enquiries: list[ResolvedEnquiry] = []

        for row in self.pages():
            if row.get("type") != ENQUIRY_TYPE:
                continue

            enquiry = read_enquiry(row)

            if enquiry is not None:
                enquiries.append(enquiry)

        return sorted(
            enquiries,
            key=lambda item: (item.created_at or "", item.enquiry_ref),
            reverse=True,
        )

    def open_page(self, limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
        """Safe metadata for the open enquiries, bounded by `limit`, and the total.

        The total is how many open enquiries there actually are, not how many
        were returned. Twenty rows out of forty-seven and twenty out of twenty
        look identical on screen, and an operator who cannot tell them apart
        will believe they have seen the queue.

        One scan answers both. Counting separately would walk the provider's
        pages a second time for a number the first walk already had, and this
        surface reads live on every press.
        """
        count = validate_limit(limit)

        enquiries = self.scan()

        return [enquiry.summary() for enquiry in enquiries[:count]], len(enquiries)

    def list_open(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Safe metadata for the open enquiries, bounded by `limit`."""
        return self.open_page(limit)[0]

    def resolve(self, enquiry_ref: object) -> ResolvedEnquiry:
        """`enquiry_ref` -> the provider identifiers, by re-listing.

        No server-side state lives between a list and a draft. A fabricated ref
        matches no real enquiry and raises, so a caller cannot reach a thread it
        was never shown.

        Raises:
            ValueError: the ref is malformed or names no open enquiry.
        """
        if not is_well_formed_enquiry_ref(enquiry_ref):
            raise ValueError(UNKNOWN_ENQUIRY)

        for enquiry in self.scan():
            if enquiry.enquiry_ref == enquiry_ref:
                return enquiry

        raise ValueError(UNKNOWN_ENQUIRY)

    def read_thread(
        self,
        enquiry_ref: object,
    ) -> tuple[ResolvedEnquiry, tuple[ConversationMessage, ...]]:
        """One enquiry and its sanitized messages, oldest first.

        Message sanitization is the booking path's `read_messages`, reused
        rather than reimplemented: one place decides which message fields exist,
        so an enquiry thread cannot leak a field a booking thread drops.
        """
        enquiry = self.resolve(enquiry_ref)

        thread = self._client.get_thread(enquiry.thread_uid)

        return enquiry, read_messages(thread)


# -- the one governed write ------------------------------------------------


def enquiry_outcome(
    enquiry_ref: str,
    status: SendStatus,
    message: str,
    messages: tuple[SentMessage, ...] = (),
) -> dict[str, Any]:
    """One send outcome, named for the thing an enquiry actually is.

    `SendOutcome`, `SendStatus` and `SentMessage` are the booked-guest path's
    own types, reused rather than duplicated: this system has three send
    outcomes, not six, and a parallel enum is how two of them start to drift.

    The only adjustment is the key. The reference this outcome carries is an
    `enquiry_ref`; emitting it as `conversation_ref` would name it after a
    booking conversation it is not, and the console would have to guess which
    one it had. The rename happens once, here, rather than in every caller.
    """
    payload = SendOutcome(
        conversation_ref=enquiry_ref,
        status=status,
        message=message,
        messages=messages,
    ).to_dict()

    payload["enquiry_ref"] = payload.pop("conversation_ref")

    return payload


class LodgifyEnquirySender:
    """Sends one approved reply to one open enquiry. Nothing else.

    Structurally the twin of `LodgifyInbox.send_reply`, and deliberately so:
    validate, snapshot, send exactly once, re-read, diff, and resolve every
    uncertain path to `UNKNOWN_SEND_STATE` -- which is not a failure, is never
    retried, and asks for a person.

    It is a separate object from `LodgifyEnquiries` so that reading enquiries
    and writing to one are different capabilities. The drafting service holds
    the reader and cannot reach this class; this class is held only by the tool
    the approval gate stands in front of.
    """

    def __init__(
        self,
        enquiries: LodgifyEnquiries,
        client: LodgifyMessagingClient,
    ) -> None:
        self._enquiries = enquiries
        self._client = client

    def send_reply(
        self,
        enquiry_ref: str,
        subject: str,
        message: str,
    ) -> dict[str, Any]:
        """Send one enquiry reply and verify it by re-reading the thread.

        Arguments are validated before anything leaves the process, so a bad
        argument costs nothing and is recoverable. Validation rejects; it never
        rewrites, so the text a human approved is transmitted byte for byte.

        Raises:
            ValueError: the ref names no open enquiry, or the text is invalid.
                Nothing was sent.
            TypeError: the subject or message was not a string. Nothing was
                sent.
        """
        checked_subject = validate_subject(subject)
        checked_message = validate_message(message)

        enquiry = self._enquiries.resolve(enquiry_ref)

        try:
            before = read_messages(self._client.get_thread(enquiry.thread_uid))

        except LodgifyUnavailable:
            # No snapshot means no way to verify afterwards. Refuse before
            # sending rather than send something we could never confirm.
            return enquiry_outcome(
                enquiry.enquiry_ref,
                SendStatus.CONFIRMED_FAILED,
                "The enquiry thread could not be read before sending, so "
                "nothing was sent.",
            )

        known_refs = {existing.message_ref for existing in before}

        try:
            # Exactly one POST, to the enquiry endpoint. There is no retry here
            # and none may be added, and there is no branch that falls back to
            # the booking endpoint.
            self._client.post_enquiry_message(
                enquiry_id=enquiry.enquiry_id,
                subject=checked_subject,
                message=checked_message,
            )

        except LodgifySendRefused as exc:
            return enquiry_outcome(
                enquiry.enquiry_ref,
                SendStatus.CONFIRMED_FAILED,
                f"Nothing was sent. {exc}",
            )

        except LodgifySendAmbiguous:
            return enquiry_outcome(
                enquiry.enquiry_ref,
                SendStatus.UNKNOWN_SEND_STATE,
                UNKNOWN_SEND_MESSAGE,
            )

        return self.verify(enquiry, checked_subject, checked_message, known_refs)

    def verify(
        self,
        enquiry: ResolvedEnquiry,
        subject: str,
        message: str,
        known_refs: set[str],
    ) -> dict[str, Any]:
        """Find the rows our send created, or admit that we cannot.

        The POST carries no identifier back, so attribution is by difference:
        rows that were not there before, from us, carrying exactly the text we
        sent -- and only when `correlated` says the set can safely be read as
        one send. Anything else is UNKNOWN_SEND_STATE rather than a guess.
        """
        try:
            after = read_messages(self._client.get_thread(enquiry.thread_uid))

        except LodgifyUnavailable:
            # The message may well have been sent -- we just cannot show it.
            return enquiry_outcome(
                enquiry.enquiry_ref,
                SendStatus.UNKNOWN_SEND_STATE,
                UNKNOWN_SEND_MESSAGE,
            )

        matches = [
            row
            for row in after
            if row.message_ref not in known_refs
            and row.sender == SENDER_OWNER
            and row.subject == subject
            and row.message == message
        ]

        if not matches or not correlated(matches):
            return enquiry_outcome(
                enquiry.enquiry_ref,
                SendStatus.UNKNOWN_SEND_STATE,
                UNKNOWN_SEND_MESSAGE,
            )

        created = tuple(
            SentMessage(
                message_ref=row.message_ref,
                message_status=row.message_status,
                created_at=row.created_at,
            )
            for row in matches
        )

        return enquiry_outcome(
            enquiry.enquiry_ref,
            SendStatus.CONFIRMED_SENT,
            summarise_delivery(created),
            created,
        )
