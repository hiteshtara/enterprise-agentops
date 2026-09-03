"""What AgentGuard does with a verified Lodgify webhook.

The governing idea: **a webhook is a hint, not a fact.** It says something
changed; it is not the record of what changed. Everything authoritative is
re-read afterwards through the supported GET APIs, which is also what makes
retries harmless -- a repeated event just re-checks the same current state.

That matters more than usual here, because the documented
`guest_message_received` payload carries `guest_name` and the full `message`
body. None of it is stored. Two fields are read -- the event name and the thread
identifier -- and the thread identifier is used only to resolve AgentGuard's own
opaque `conversation_ref` before being discarded.

A webhook can never cause a send. It resolves a reference and stops; drafting
and approval are unchanged and still start with a person.
"""

import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import LodgifyInbox

# The 10 subscribable events, read from Lodgify's own subscribe endpoint
# documentation. Listed so an unrecognised `action` is visibly unknown rather
# than quietly treated as one of ours.
KNOWN_EVENTS: tuple[str, ...] = (
    "rate_change",
    "availability_change",
    "booking_new_any_status",
    "booking_new_status_booked",
    "booking_change",
    "booking_status_change_booked",
    "booking_status_change_tentative",
    "booking_status_change_open",
    "booking_status_change_declined",
    "guest_message_received",
)

GUEST_MESSAGE_RECEIVED = "guest_message_received"

# Recent events are kept in memory only, for operating visibility during this
# discovery phase. Nothing is persisted: correctness does not depend on webhook
# history, because the action a webhook triggers -- re-reading current state --
# is idempotent, so a lost or repeated event costs nothing.
RECENT_EVENT_LIMIT = 50


@dataclass(frozen=True)
class WebhookReceipt:
    """The safe record of one verified event.

    Field-by-field, like every other provider projection here. `guest_name`,
    `message`, `subject` and `message_id` from the payload are read nowhere.
    """

    event_type: str
    known_event: bool
    received_at: str
    conversation_ref: str | None
    resolved: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "known_event": self.known_event,
            "received_at": self.received_at,
            "conversation_ref": self.conversation_ref,
            "resolved": self.resolved,
            "detail": self.detail,
        }


class WebhookLog:
    """A short in-memory tail of verified events. Never contains guest data."""

    def __init__(self, limit: int = RECENT_EVENT_LIMIT) -> None:
        self._events: deque[WebhookReceipt] = deque(maxlen=limit)

    def record(self, receipt: WebhookReceipt) -> None:
        self._events.appendleft(receipt)

    def recent(self) -> list[dict[str, Any]]:
        return [receipt.to_dict() for receipt in self._events]

    def clear(self) -> None:
        self._events.clear()


def unwrap_event(payload: object) -> dict[str, Any] | None:
    """The event object, however the delivery wrapped it.

    Verified live 2026-09-03: Lodgify delivers the event as a **JSON array**
    containing one object -- `[{"action": ...}]` -- while the published example
    on /reference/webhook-objects shows a bare object. Both are accepted, since
    the documentation is the thing more likely to change than the wire format,
    and a receiver that only understood the documented shape silently dropped
    every real event.
    """
    if isinstance(payload, dict):
        return payload

    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("action"):
                return item

    return None


def event_name_of(payload: object) -> str:
    """The event, from the documented `action` field."""
    event = unwrap_event(payload)

    if event is None:
        return "unparsable"

    action = event.get("action")

    return action if isinstance(action, str) and action else "unknown"


def thread_uid_of(payload: object) -> str | None:
    """The one identifier worth reading, and only to resolve our own ref."""
    event = unwrap_event(payload)

    if event is None:
        return None

    value = event.get("thread_uid")

    return value if isinstance(value, str) and value else None


def handle_event(
    payload: object,
    inbox: LodgifyInbox | None,
    now: str | None = None,
) -> WebhookReceipt:
    """Turn a verified payload into a safe receipt.

    Never raises for a provider problem: Lodgify retries a non-200 up to ten
    times, and a temporary read failure on our side is not something worth being
    retried at. The event is acknowledged, nothing is invented, and the ordinary
    Inbox refresh picks the conversation up later.
    """
    received_at = now or datetime.now(UTC).isoformat()

    event_type = event_name_of(payload)
    known = event_type in KNOWN_EVENTS

    if not known:
        return WebhookReceipt(
            event_type=event_type,
            known_event=False,
            received_at=received_at,
            conversation_ref=None,
            resolved=False,
            detail="Unrecognised event. Acknowledged and ignored.",
        )

    if event_type != GUEST_MESSAGE_RECEIVED:
        # Booking, rate and availability events are real and subscribable, but
        # nothing in AgentGuard acts on them yet. Acknowledge honestly.
        return WebhookReceipt(
            event_type=event_type,
            known_event=True,
            received_at=received_at,
            conversation_ref=None,
            resolved=False,
            detail="Known event with no handler yet. Acknowledged.",
        )

    thread_uid = thread_uid_of(payload)

    if thread_uid is None:
        return WebhookReceipt(
            event_type=event_type,
            known_event=True,
            received_at=received_at,
            conversation_ref=None,
            resolved=False,
            detail="No thread reference in the event; nothing to resolve.",
        )

    if inbox is None:
        return WebhookReceipt(
            event_type=event_type,
            known_event=True,
            received_at=received_at,
            conversation_ref=None,
            resolved=False,
            detail="The Lodgify connector is not configured.",
        )

    try:
        # The authoritative step: the thread is matched against this account's
        # own bookings, across the whole archive rather than just recent ones.
        # A thread we do not recognise resolves to nothing, which is also what
        # stops a forged-but-signed payload naming someone else's conversation.
        match = inbox.find_by_thread(thread_uid)

    except LodgifyUnavailable:
        return WebhookReceipt(
            event_type=event_type,
            known_event=True,
            received_at=received_at,
            conversation_ref=None,
            resolved=False,
            detail=(
                "The provider could not be read just now. Acknowledged; the "
                "Inbox refresh will pick this conversation up."
            ),
        )

    if match is None:
        return WebhookReceipt(
            event_type=event_type,
            known_event=True,
            received_at=received_at,
            conversation_ref=None,
            resolved=False,
            detail="The thread does not match any known booking.",
        )

    return WebhookReceipt(
        event_type=event_type,
        known_event=True,
        received_at=received_at,
        conversation_ref=match.conversation_ref,
        resolved=True,
        detail=(
            "Conversation identified. Its current state will be re-read through "
            "the supported API; nothing was sent."
        ),
    )


# Lodgify's documented examples are JSON objects, but a webhook sender is not
# obliged to use the content type you expect, and a receiver that only
# understands one encoding silently drops real events. So the body is decoded by
# trying the shapes a webhook realistically arrives in, cheapest first.
def parse_webhook_body(body: bytes, content_type: object = None) -> object | None:
    """Decode a verified body into a payload object, or None.

    Signature verification has already happened against the raw bytes, so
    nothing here can weaken it -- this only decides how to *read* what was
    proven authentic.
    """
    if not body:
        return None

    text = body.decode("utf-8", errors="replace").strip()

    try:
        return json.loads(text)

    except ValueError:
        pass

    # Form-encoded, with the event either as fields or as JSON in one field.
    parsed = parse_qs(text, keep_blank_values=True)

    if parsed:
        flat = {key: values[0] for key, values in parsed.items() if values}

        for key in ("payload", "body", "data", "event"):
            candidate = flat.get(key)

            if isinstance(candidate, str) and candidate.lstrip().startswith("{"):
                try:
                    return json.loads(candidate)

                except ValueError:
                    continue

        if "action" in flat:
            return flat

    return None
