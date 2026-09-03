"""The guest-messaging capabilities exposed to the agent.

Three tools. Two read, one sends.

What the model may name is the whole design here. It chooses a
`conversation_ref` and, for a send, the text. It cannot name a booking, a
thread, a sender type, or whether anyone gets notified -- those are resolved or
pinned below this layer. See docs/LODGIFY_API.md sections 6 and 18.

There is deliberately **no drafting tool**. A tool that called the model to
write a reply would be a model call wearing a tool's clothes: it would hide the
reasoning from the run trace, spend a second inference nobody asked for, and
make the draft look like a deterministic result. The model reads the
conversation, and the model writes the draft. `get_guest_conversation` returns
the house rules alongside the messages so it has what it needs.
"""

from typing import Any

from app.connectors.lodgify.config import LODGIFY_SLUGS
from app.connectors.lodgify.errors import (
    LodgifyConfigurationError,
    LodgifyUnavailable,
)
from app.connectors.lodgify.inbox import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_MESSAGE_LENGTH,
    MAX_SUBJECT_LENGTH,
    MIN_LIMIT,
    LodgifyInbox,
)
from app.connectors.lodgify.models import unknown
from app.hospitality import reply_guidance


class LodgifyMessagingTools:
    """Adapter between the tool registry and the inbox service."""

    def __init__(self, inbox: LodgifyInbox) -> None:
        self._inbox = inbox

    def list_recent_guest_conversations(
        self,
        property_slug: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        try:
            conversations = self._inbox.list_conversations(
                property_slug=property_slug,
                limit=limit,
            )

        except LodgifyConfigurationError:
            return unknown(
                "not_configured",
                "The Lodgify connector is not configured, so conversations "
                "cannot be listed.",
            )

        except LodgifyUnavailable as exc:
            return unknown(
                "provider_unavailable",
                f"Conversations could not be listed: {exc}",
            )

        return {
            "ok": True,
            "conversations": conversations,
            "count": len(conversations),
        }

    def get_guest_conversation(self, conversation_ref: str) -> dict[str, Any]:
        try:
            conversation = self._inbox.get_conversation(conversation_ref)

        except LodgifyConfigurationError:
            return unknown(
                "not_configured",
                "The Lodgify connector is not configured, so this conversation "
                "cannot be read.",
            )

        except LodgifyUnavailable as exc:
            return unknown(
                "provider_unavailable",
                f"The conversation could not be read: {exc}",
                conversation_ref=conversation_ref,
            )

        # House rules travel with the conversation so drafting never depends on
        # the model remembering to look them up. The guidance is computed *from*
        # the messages, so it reports what is still open rather than every topic
        # the thread has ever mentioned.
        return {
            "ok": True,
            **conversation,
            "reply_guidance": reply_guidance(conversation.get("messages")),
        }

    def send_guest_reply(
        self,
        conversation_ref: str,
        subject: str,
        message: str,
    ) -> dict[str, Any]:
        """Send one reply. Reached only through an approved DANGEROUS tool call.

        A provider failure is returned, not raised: the three send outcomes are
        the contract, and UNKNOWN_SEND_STATE in particular must reach the caller
        intact rather than becoming a generic tool failure.
        """
        try:
            return self._inbox.send_reply(
                conversation_ref=conversation_ref,
                subject=subject,
                message=message,
            )

        except LodgifyConfigurationError:
            return unknown(
                "not_configured",
                "The Lodgify connector is not configured, so nothing was sent.",
            )


# -- tool schemas ---------------------------------------------------------

CONVERSATION_REF_SCHEMA = {
    "type": "string",
    "description": (
        "The conversation to act on, exactly as returned by "
        "list_recent_guest_conversations. Opaque -- never construct or guess "
        "one."
    ),
}

LIST_CONVERSATIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "property_slug": {
            "type": "string",
            "enum": list(LODGIFY_SLUGS),
            "description": (
                "Optional. Restrict to one property. Omit for every property."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": MIN_LIMIT,
            "maximum": MAX_LIMIT,
            "description": (
                f"How many conversations to return. Defaults to {DEFAULT_LIMIT}."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

GET_CONVERSATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"conversation_ref": CONVERSATION_REF_SCHEMA},
    "required": ["conversation_ref"],
    "additionalProperties": False,
}

# Exactly three fields. `type` and `send_notification` are pinned in the
# connector and are absent here on purpose: they decide who a message appears to
# come from and whether anyone is told about it, which is not the model's call.
# `booking_id` and `thread_uid` are absent for the same reason as everywhere
# else -- a model that can name one can address an arbitrary reservation.
SEND_REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conversation_ref": CONVERSATION_REF_SCHEMA,
        "subject": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SUBJECT_LENGTH,
            "description": (
                "Short single-line subject for the message. Plain text only."
            ),
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_MESSAGE_LENGTH,
            "description": (
                "The exact text the guest will receive. Plain text only, no "
                "HTML. This is sent verbatim once approved -- write it as it "
                "should be read."
            ),
        },
    },
    "required": ["conversation_ref", "subject", "message"],
    "additionalProperties": False,
}
