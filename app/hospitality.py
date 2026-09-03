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
    # Amenities and equipment: whether a specific item is in a specific unit is
    # not recorded anywhere. A draft once said "we should have a wine bottle
    # opener available" -- turning a guess into a promise about a real stay.
    "whether a specific amenity, appliance or item is present in a unit",
)


DRAFTING_GUIDANCE = (
    "Write as the host of Priyanka Homes: warm, brief, concrete. One to three "
    "sentences is usually right, and one is often enough. Answer the question "
    "first, then stop. "
    "Do not repeat the guest's question back to them, do not summarise the "
    "conversation, and do not add an explanation the guest did not ask for. "
    "Do not open with 'I hope this message finds you well'. Do not sign off "
    "with a name -- the message already comes from the host. "
    "Never quote a price: pricing comes from the quote tool, never from memory."
)

# Boilerplate that reads as a support queue rather than a host. Listed rather
# than described, because a model matches examples more reliably than adjectives.
AVOID_PHRASES: tuple[str, ...] = (
    "Thank you for reaching out regarding...",
    "We appreciate your inquiry...",
    "Please do not hesitate to...",
    "We'll do our best to accommodate your request...",
    "I hope this message finds you well...",
    "As per our previous message...",
)


# -- conversation state ---------------------------------------------------
#
# The topic rules above are indexed by subject, which is exactly why they are
# not sufficient on their own: a thread that once mentioned early check-in will
# match the early-check-in rule forever, whether or not it was already answered.
# Everything below exists to answer a different question -- *what, if anything,
# still needs a reply right now*.

# The exact token a draft uses to say that sending anything would add no value.
# Matched literally by the console, so it must not change casually.
NO_REPLY_NEEDED = "NO_REPLY_NEEDED"

# Short closings that carry no new request. A guest saying one of these is
# ending the exchange, not reopening it.
ACKNOWLEDGEMENT_PHRASES: frozenset[str] = frozenset(
    {
        "thank you",
        "thanks",
        "thank you so much",
        "thanks so much",
        "thank you very much",
        "many thanks",
        "thx",
        "ty",
        "sounds good",
        "sounds great",
        "ok",
        "okay",
        "perfect",
        "great",
        "awesome",
        "got it",
        "understood",
        "appreciate it",
        "much appreciated",
        "will do",
        "see you then",
        "see you soon",
        "no problem",
        "no worries",
    }
)

# An acknowledgement is short by nature. A longer message that merely opens with
# "thanks" is usually carrying a new request behind it.
MAX_ACKNOWLEDGEMENT_WORDS = 6

CONVERSATION_STATE_RULES: tuple[str, ...] = (
    "Read the whole conversation in order before writing anything.",
    "Find the guest's most recent message. That is what you are replying to.",
    (
        "List every question, request or problem the guest has raised across the "
        "thread, then cross off each one a later Owner message already answered."
    ),
    (
        "Reply only to what is still open. A question the host already answered "
        "is closed -- do not answer it again, do not restate the answer, and do "
        "not mention the topic at all unless the guest brings it back up."
    ),
    (
        "Answer a closed question again only if the guest asks again, or the "
        "earlier answer was explicitly conditional and you now have a confirmed "
        "fact to replace it with."
    ),
    (
        "If the guest's latest message is an acknowledgement and nothing is "
        "open, either send one short friendly closing line or reply with "
        f"{NO_REPLY_NEEDED}. Do not manufacture an operational explanation just "
        "because earlier messages contained questions."
    ),
    (
        "A conditional answer ('we'll know tonight') is not resolved, but it is "
        "also not a reason to repeat yourself. Wait for new information or for "
        "the guest to ask again."
    ),
)

NO_REPLY_GUIDANCE = (
    f"Reply with exactly {NO_REPLY_NEEDED} -- and nothing else -- when another "
    "message would add no value: the guest's last message is a thank-you or "
    "similar closing, and nothing is left open. This is a good outcome, not a "
    "failure. Silence is often the right answer to 'Thank you!', and a host who "
    "replies to every acknowledgement is a host the guest stops reading. "
    "It is never correct when the guest's latest message contains a new "
    "question or request, however politely it is wrapped."
)


def normalise_acknowledgement(text: str) -> str:
    """Reduce a message to comparable words: lowercase, letters and spaces only.

    Strips punctuation and emoji so "Thank you!!" and "thank you 🙏" both land on
    "thank you".
    """
    kept = [character.lower() if character.isalnum() else " " for character in text]

    return " ".join("".join(kept).split())


def is_acknowledgement(text: str) -> bool:
    """Whether a guest message is a closing courtesy carrying no new request.

    Conservative in the direction that matters: anything with a question mark,
    and anything longer than a short phrase, is *not* an acknowledgement. The
    cost of missing one is a slightly unnecessary reply; the cost of a false
    positive is ignoring a real question.
    """
    if "?" in text:
        return False

    normalised = normalise_acknowledgement(text)

    if not normalised:
        return False

    if normalised in ACKNOWLEDGEMENT_PHRASES:
        return True

    words = normalised.split()

    if len(words) > MAX_ACKNOWLEDGEMENT_WORDS:
        return False

    # "thank you so much!" and "ok great thanks" -- built only from
    # acknowledgement words, so still carrying no request.
    return all(
        any(word in phrase.split() for phrase in ACKNOWLEDGEMENT_PHRASES)
        for word in words
    )


def analyse_conversation(messages: Any) -> dict[str, Any]:
    """What still needs a reply, computed rather than inferred.

    The model is good at wording and bad at bookkeeping, so the bookkeeping is
    done here: which guest messages arrived after our last reply, whether the
    latest one is merely a courtesy, and whether anything is open at all. The
    model gets facts instead of being asked to re-derive them from a transcript.

    `suggested_outcome` is advice, not a decision. The model still reads the
    conversation and may disagree -- it can see that a politely-worded
    acknowledgement carries a request, which this function cannot.

    Expects sanitized messages in chronological order.
    """
    rows = [
        {
            "sender": message.get("sender"),
            "message": message.get("message") or "",
            "created_at": message.get("created_at"),
        }
        for message in (messages or [])
        if isinstance(message, dict)
    ]

    guest_rows = [row for row in rows if row["sender"] == "Renter"]

    last_owner_index = max(
        (index for index, row in enumerate(rows) if row["sender"] == "Owner"),
        default=-1,
    )

    # Guest messages that arrived after our last reply are the only ones that
    # can still be open. Anything before it, we have already responded to.
    unanswered = [
        row
        for index, row in enumerate(rows)
        if index > last_owner_index and row["sender"] == "Renter"
    ]

    latest_guest = guest_rows[-1] if guest_rows else None

    latest_is_acknowledgement = bool(
        latest_guest and is_acknowledgement(latest_guest["message"])
    )

    if not unanswered:
        outcome = "already_replied"

    elif all(is_acknowledgement(row["message"]) for row in unanswered):
        outcome = "no_reply_needed"

    else:
        outcome = "reply_needed"

    return {
        "message_count": len(rows),
        "latest_sender": rows[-1]["sender"] if rows else None,
        "awaiting_our_reply": bool(unanswered),
        "unanswered_guest_messages": [row["message"] for row in unanswered],
        "latest_guest_message": latest_guest["message"] if latest_guest else None,
        "latest_guest_message_is_acknowledgement": latest_is_acknowledgement,
        "answered_earlier_by_us": [
            row["message"]
            for index, row in enumerate(rows)
            if index < last_owner_index and row["sender"] == "Renter"
        ],
        "suggested_outcome": outcome,
        "note": (
            "answered_earlier_by_us lists guest messages a later Owner message "
            "already responded to. Do not answer those again. suggested_outcome "
            "is advice from a simple rule -- read the conversation and override "
            "it if the wording tells you otherwise."
        ),
    }


def reply_guidance(messages: Any = ()) -> dict[str, Any]:
    """The knowledge a model needs to draft a Priyanka Homes reply.

    Returned alongside a conversation rather than exposed as its own tool: the
    model should never have to decide whether to go and look up the house rules
    before answering a guest. It gets them with the conversation, every time.

    `conversation_state` is included for the same reason. Topic rules alone
    produce a reply about every subject the thread has ever touched; the state
    block is what makes a reply about the subject still open.
    """
    return {
        "conversation_state": analyse_conversation(messages),
        "how_to_read_the_conversation": list(CONVERSATION_STATE_RULES),
        "no_reply_needed": NO_REPLY_GUIDANCE,
        "voice": DRAFTING_GUIDANCE,
        "avoid_phrases": list(AVOID_PHRASES),
        "acknowledgement": ACKNOWLEDGEMENT,
        "rules": [rule.to_dict() for rule in REPLY_RULES],
        "topic_rules_are_conditional": (
            "The rules below say how to answer a topic *if it is open*. They are "
            "not a checklist of things to mention. Never introduce a topic the "
            "guest's outstanding message did not raise."
        ),
        "do_not_answer_from_memory": list(UNDOCUMENTED_TOPICS),
        "never_invent": (
            "State a property fact only when it is supported by this guidance, "
            "by something already said in this conversation, or by an "
            "authoritative tool result. If you do not know whether a unit has "
            "something, say you will check -- never soften a guess into a "
            "promise with words like 'should have' or 'I believe'."
        ),
        "escalation": (
            "If the question touches anything in do_not_answer_from_memory, or "
            "anything the rules do not cover, use the acknowledgement and let a "
            "person answer. An honest 'I'll check' is always acceptable; a "
            "confident guess is not."
        ),
    }
