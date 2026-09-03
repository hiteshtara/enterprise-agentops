"""The only conversation shapes this connector will emit.

Every field is constructed explicitly from a named upstream field. There is no
passthrough, no `**rest`, no `dict(response)`. The upstream thread object
carries `guest_name` and `guest_email`, and the upstream booking object carries
a guest block, contact details, financial fields and an IP address -- none of it
can reach the model, the trace, the audit log or the browser by accident,
because none of it is ever read.

Two provider identifiers are deliberately absent from every shape here:
`booking_id` and `thread_uid`. They live only inside the inbox service. See
docs/LODGIFY_API.md sections 6 and 21.
"""

import base64
import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Sender types observed live. `Owner` is the property operator (outbound),
# `Renter` is the guest (inbound). See docs/LODGIFY_API.md section 7.
SENDER_OWNER = "Owner"

SENDER_RENTER = "Renter"

KNOWN_SENDERS = (SENDER_OWNER, SENDER_RENTER)

# Delivery statuses observed live. Anything unrecognised is normalised to
# UNKNOWN rather than passed through, so a new upstream value cannot be
# rendered as if it meant something.
KNOWN_MESSAGE_STATUSES = ("Delivered", "Sent", "Failed", "Unknown")

MESSAGE_REF_NAMESPACE = b"agentguard.message.v1"

MESSAGE_REF_DIGEST_BYTES = 5

EXCERPT_LENGTH = 160


class ConversationStatus(str, Enum):
    """How much attention a conversation appears to need.

    Deliberately three-valued. `UNKNOWN` exists so an uncertain classification
    is never silently reported as "needs a reply" -- see the rule documented on
    `classify_conversation`.
    """

    NEEDS_ATTENTION = "needs_attention"
    RESPONDED = "responded"
    UNKNOWN = "unknown"


class SendStatus(str, Enum):
    """The three outcomes of an irreversible send.

    `UNKNOWN_SEND_STATE` is not a failure and is never safe to retry: the
    message may already have reached a real guest. See docs/LODGIFY_API.md
    section 17.
    """

    CONFIRMED_SENT = "confirmed_sent"
    CONFIRMED_FAILED = "confirmed_failed"
    UNKNOWN_SEND_STATE = "unknown_send_state"


def conversation_fingerprint(messages: Any) -> str:
    """A deterministic identity for one conversation state.

    Built from the sanitized message references in order, so any new message --
    from the guest or from us -- yields a different fingerprint. That symmetry
    matters: our own send has to change the fingerprint too, or a prepared reply
    would survive its own delivery.

    Deliberately not a timestamp: a thread re-read a second later must
    fingerprint identically, or the cost control built on this is worthless.
    Built from message refs, which are already opaque, so no provider identifier
    is involved.
    """
    refs = [
        message.get("message_ref", "")
        for message in (messages or [])
        if isinstance(message, dict)
    ]

    return hashlib.sha256("\x1f".join(refs).encode("utf-8")).hexdigest()[:32]


def message_ref_for(message_id: object) -> str:
    """A stable, opaque reference for one message row.

    The upstream row carries both an integer `id` and a UUID `message_id`. Both
    are provider identifiers, so neither is emitted; this digest is what a
    caller sees instead. It exists so a UI can key a list and a reader can tell
    two messages apart -- not so anything can be addressed by it.
    """
    digest = hashlib.blake2s(
        str(message_id).encode("utf-8"),
        person=MESSAGE_REF_NAMESPACE[:8],
        digest_size=MESSAGE_REF_DIGEST_BYTES,
    ).digest()

    return "m-" + base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def normalise_sender(value: object) -> str | None:
    """The sender type, or None when upstream sent something unrecognised."""
    return value if value in KNOWN_SENDERS else None


def normalise_message_status(value: object) -> str | None:
    """The delivery status, or None when absent or unrecognised.

    None means "the provider did not tell us", which is different from the
    provider's own literal `"Unknown"`. Both are honest; neither is success.
    """
    return value if value in KNOWN_MESSAGE_STATUSES else None


def excerpt(text: str) -> str:
    """A short single-line preview of a message body for the list view."""
    collapsed = " ".join(text.split())

    if len(collapsed) <= EXCERPT_LENGTH:
        return collapsed

    return collapsed[: EXCERPT_LENGTH - 1].rstrip() + "…"


@dataclass(frozen=True)
class ConversationMessage:
    """One message in a thread, sanitized.

    `route` is deliberately not a field. A live send recorded `route: null`
    while really being delivered, so route cannot support any delivery claim --
    see docs/LODGIFY_API.md section 12.
    """

    message_ref: str
    sender: str | None
    subject: str | None
    message: str
    created_at: str | None
    message_status: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_ref": self.message_ref,
            "sender": self.sender,
            "subject": self.subject,
            "message": self.message,
            "created_at": self.created_at,
            "message_status": self.message_status,
        }


@dataclass(frozen=True)
class ConversationSummary:
    """One row of the inbox list."""

    conversation_ref: str
    property_slug: str | None
    property_name: str | None
    source: str | None
    booking_status: str | None
    status: ConversationStatus
    last_message_at: str | None
    last_message_sender: str | None
    last_message_excerpt: str | None
    message_count: int
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_ref": self.conversation_ref,
            "fingerprint": self.fingerprint,
            "property_slug": self.property_slug,
            "property_name": self.property_name,
            "source": self.source,
            "booking_status": self.booking_status,
            "status": self.status.value,
            "last_message_at": self.last_message_at,
            "last_message_sender": self.last_message_sender,
            "last_message_excerpt": self.last_message_excerpt,
            "message_count": self.message_count,
        }


@dataclass(frozen=True)
class Conversation:
    """One full thread, chronological."""

    conversation_ref: str
    property_slug: str | None
    property_name: str | None
    source: str | None
    booking_status: str | None
    subject: str | None
    is_read: bool | None
    status: ConversationStatus
    messages: tuple[ConversationMessage, ...]
    # About this guest's own reservation, derived from authoritative booking
    # state rather than from anything said in the thread. `None` means the
    # booking state could not be established -- never "not cancelled".
    booking_cancelled: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_ref": self.conversation_ref,
            "property_slug": self.property_slug,
            "property_name": self.property_name,
            "source": self.source,
            "booking_status": self.booking_status,
            "booking_cancelled": self.booking_cancelled,
            "subject": self.subject,
            "is_read": self.is_read,
            "status": self.status.value,
            "messages": [message.to_dict() for message in self.messages],
        }


@dataclass(frozen=True)
class SentMessage:
    """One newly-created row attributed to a send.

    A single send can produce several of these -- one per configured route on
    the thread. See docs/LODGIFY_API.md section 14.
    """

    message_ref: str
    message_status: str | None
    created_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_ref": self.message_ref,
            "message_status": self.message_status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SendOutcome:
    conversation_ref: str
    status: SendStatus
    message: str
    messages: tuple[SentMessage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "conversation_ref": self.conversation_ref,
            "message": self.message,
            "messages": [message.to_dict() for message in self.messages],
        }
