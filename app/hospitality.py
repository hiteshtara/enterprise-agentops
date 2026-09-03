"""Priyanka Homes reply knowledge.

Deliberately separate from the Lodgify connector. This module holds *what the
business will say*; `app/connectors/lodgify/` holds *how a message travels*.
Mixing them would mean a transport change could quietly alter a guest-facing
promise, and a policy change would need a connector review.

Everything here is either explicitly documented in repository knowledge or was
supplied directly by the owner. **Nothing is invented.** The hardest rule in
this file is the last one: topics with no recorded answer get an honest
"I'll check", never a plausible guess. A confident wrong answer about a refund
or a pet policy is worse for the business than a short delay.

This is meant to be read and edited by a person. Add a rule when the owner
states one -- not when a guest asks a question we happen not to cover.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReplyRule:
    """One topic the assistant is allowed to speak to."""

    topic: str
    guidance: str
    example: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "guidance": self.guidance,
            "example": self.example,
        }


# The safe default. Any question this file does not cover gets acknowledged and
# handed to a person -- see UNDOCUMENTED_TOPICS below.
ACKNOWLEDGEMENT = "Thank you for your question. I'll check and get back to you shortly."


REPLY_RULES: tuple[ReplyRule, ...] = (
    ReplyRule(
        topic="general_acknowledgement",
        guidance=(
            "When the guest's question cannot be answered from the rules below, "
            "acknowledge it warmly and say a person will follow up. Never "
            "improvise an answer to fill the gap."
        ),
        example=ACKNOWLEDGEMENT,
    ),
    ReplyRule(
        topic="early_check_in",
        guidance=(
            "Never guarantee early check-in automatically. Whether it is "
            "possible depends on the previous guest's checkout and on cleaning "
            "being finished. Say that it depends on those two things, that we "
            "will try, and that we will confirm once the unit is ready. Do not "
            "state a specific earlier time as if it were agreed."
        ),
        example=(
            "We'll do our best to get you in early. It depends on the previous "
            "guest's checkout and on cleaning being finished, so I can't "
            "promise a time yet -- I'll confirm as soon as the unit is ready."
        ),
    ),
    ReplyRule(
        topic="late_checkout",
        guidance=(
            "Never guarantee late checkout automatically. It depends on the "
            "next arrival and on the cleaning schedule. Say that we will check "
            "and confirm. Do not state a specific later time as if it were "
            "agreed."
        ),
        example=(
            "I'll check whether a later checkout works with the next arrival "
            "and the cleaning schedule, and confirm as soon as I know."
        ),
    ),
    ReplyRule(
        topic="parking",
        guidance=(
            "Shared parking may be available where applicable, and there is no "
            "extra charge when it is shared. Property-specific rules always "
            "override this general rule: if the guest's property has its own "
            "recorded parking arrangement, that wins. Do not state that parking "
            "is guaranteed, reserved, or available at a specific property "
            "unless that is recorded for the property."
        ),
        example=(
            "Parking is shared and there's no extra charge for it. Let me "
            "confirm the arrangement for your specific unit and come back to you."
        ),
    ),
)


# Topics a guest will plausibly ask about that this file has no recorded answer
# for. Naming them explicitly is the point: an assistant that knows *which*
# questions it cannot answer will acknowledge rather than invent. Move a topic
# out of this list only when the owner states the actual policy.
UNDOCUMENTED_TOPICS: tuple[str, ...] = (
    "refund and cancellation policy",
    "pet policy",
    "specific check-in and check-out times",
    "per-property parking availability, reserved spaces, or driveway access",
    "early check-in or late checkout that has already been agreed",
    "cleaning fees, deposits, taxes, or any price",
    "wifi passwords, lockbox codes, or any access credential",
    "local recommendations presented as endorsements",
)


DRAFTING_GUIDANCE = (
    "Write as the host of Priyanka Homes: warm, brief, concrete. Two to four "
    "sentences is usually right. Do not open with 'I hope this message finds "
    "you well'. Do not sign off with a name -- the message already comes from "
    "the host. Never quote a price: pricing comes from the quote tool, never "
    "from memory. Never promise anything the rules below say to confirm later."
)


def reply_guidance() -> dict[str, Any]:
    """The knowledge a model needs to draft a Priyanka Homes reply.

    Returned alongside a conversation rather than exposed as its own tool: the
    model should never have to decide whether to go and look up the house rules
    before answering a guest. It gets them with the conversation, every time.
    """
    return {
        "voice": DRAFTING_GUIDANCE,
        "acknowledgement": ACKNOWLEDGEMENT,
        "rules": [rule.to_dict() for rule in REPLY_RULES],
        "do_not_answer_from_memory": list(UNDOCUMENTED_TOPICS),
        "escalation": (
            "If the question touches anything in do_not_answer_from_memory, or "
            "anything the rules do not cover, use the acknowledgement and let a "
            "person answer. An honest 'I'll check' is always acceptable; a "
            "confident guess is not."
        ),
    }
