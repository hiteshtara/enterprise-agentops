"""On-demand enquiry reply drafting.

The operator is looking at an enquiry, presses Generate reply, and gets text to
read and copy. That is the whole feature, and its boundaries are the point:

  * **Nothing is persisted.** No draft row, no activity row, no schema. The
    text exists in the HTTP response and nowhere else, which is why pressing
    the button twice costs two model calls and leaves no history to reconcile.
  * **Nothing is sent, and nothing can be.** There is no send path from this
    module -- `LodgifyEnquiries` has no write method and this service never
    reaches the messaging client's one POST. The absence is the safety
    property.
  * **Nothing runs on its own.** No discovery, no polling, no proactive pass.
    A person pressing a button is the only trigger.

The knowledge is the booked-guest pipeline's knowledge, with one owner-approved
divergence: `enquiry_reply_guidance` replaces the four keys that told a model to
stay silent about a business-sensitive question, and passes `never_invent`,
`authority_order`, `do_not_answer_from_memory` and the rules through untouched.
The reason the divergence is safe is the path rather than the content --
nothing here is sent and a person reads every word -- and the reason it is
necessary is that an enquiry is almost by definition business-sensitive. A
stranger asks whether the dates are free and what it costs; a pipeline that
declines every business-sensitive question declines every enquiry, which is the
one thing this feature exists to do.

What did *not* move is what may be asserted. An enquiry draft may withhold
more than a booked-guest draft, never claim more: where the answer needs a fact
AgentGuard cannot establish, the draft says it will confirm rather than
guessing, and rather than saying nothing at all.

Who decides whether a reply is owed is also not the model's call. The
deterministic `analyse_conversation` verdict travels with the request and wins:
a thread with an open question cannot come back as silence, whatever the model
answers. That is the same division of labour the booked-guest refresh enforces,
for the same reason -- an unnecessary draft costs a glance, a withheld reply
costs an enquiry.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.agent import AgentService
from app.connectors.lodgify.enquiries import LodgifyEnquiries
from app.connectors.lodgify.errors import (
    LodgifyConfigurationError,
    LodgifyUnavailable,
)
from app.connectors.lodgify.messaging_models import ConversationMessage
from app.hospitality import NO_REPLY_NEEDED, enquiry_reply_guidance
from app.knowledge import KnowledgeStore

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT = "Re: your enquiry"

# The deterministic verdict from `analyse_conversation` that means the enquirer
# is waiting on us. Spelled here rather than imported from the booked-guest
# refresh service: the same string, but this path must not depend on that
# module's constants.
REPLY_NEEDED = "reply_needed"

DRAFTED_DETAIL = (
    "Draft prepared from the house reply rules and owner-approved knowledge. "
    "Read it, edit it, and copy it into Lodgify yourself -- AgentGuard will not "
    "send it."
)

UNREADABLE_DETAIL = (
    "This enquiry's thread could not be read, so nothing was drafted. Open it "
    "in Lodgify and reply there."
)

MODEL_FAILED_DETAIL = (
    "A reply could not be drafted for this enquiry. Try again, or write one by hand."
)

# Reachable only when the analyser says nothing is open, so it can say the one
# true thing rather than the old line about the approved rules -- which was
# wrong twice over: it fired on threads that did have an open question, and it
# blamed the rules for what was really a refusal to draft.
NOTHING_TO_SAY_DETAIL = (
    "This enquiry's latest message closes the exchange and nothing is waiting "
    "on an answer, so no reply was drafted."
)

# The model declined over an open question. Not the same failure as an empty
# answer and not the same as "nothing is open", so it does not borrow either
# wording: the operator is told a reply *is* owed and that they have to write
# it, which is exactly what happened.
DECLINED_DETAIL = (
    "This enquiry has an open question, but no draft could be produced for it. "
    "Try again, or reply by hand."
)

# How the thread is rendered into the prompt. The sender vocabulary is the
# connector's own -- "Owner" for us, anything else for the enquirer -- and it is
# spelled out rather than passed through, so an unexpected upstream value cannot
# make one of our own messages read as theirs.
SENDER_OWNER = "Owner"

US_LABEL = "Us"

THEM_LABEL = "Enquirer"


@dataclass(frozen=True)
class EnquiryReplyDraft:
    """One generated draft, or the honest reason there is not one.

    `message` is None whenever anything went wrong. There is no branch that
    fills it with a fallback, an apology or a guess: an operator who is handed
    text has been handed text a model actually wrote for this thread.
    """

    enquiry_ref: str
    subject: str | None
    message: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "enquiry_ref": self.enquiry_ref,
            "subject": self.subject,
            "message": self.message,
            "detail": self.detail,
        }


def render_thread(messages: tuple[ConversationMessage, ...]) -> str:
    """The conversation as plain labelled lines, oldest first.

    Only the sanitized fields exist by this point -- `read_messages` dropped
    everything else -- so this is a rendering step and not a filtering one.
    """
    lines = [
        f"[{US_LABEL if message.sender == SENDER_OWNER else THEM_LABEL}] "
        f"{message.message}".strip()
        for message in messages
    ]

    return "\n".join(lines)


# What the model is allowed to do with the last line of the prompt, chosen by
# the analyser rather than offered as a menu.
#
# The old prompt ended "...or exactly NO_REPLY_NEEDED if nothing can be
# answered safely", on every enquiry. That single clause is what produced the
# measured behaviour: an enquiry is business-sensitive by nature, the guidance
# says a business-sensitive question needs a person, and so "nothing can be
# answered safely" read as true for almost every thread. Offering the escape
# hatch only when the analyser says nothing is open removes the contradiction
# at its source, and the runtime guard in `draft` catches the model that
# ignores it anyway.
OPEN_THREAD_INSTRUCTION = (
    "The enquirer is waiting on a reply: conversation_state says something is "
    "still open. Write one. If the answer needs a fact you cannot establish "
    "from this guidance, use the matching example in holding_replies -- "
    "acknowledge what they asked and say you will confirm it. Do NOT answer "
    f"{NO_REPLY_NEEDED}, and do not decline because the question touches "
    "price, dates, availability or the unit.\n\n"
    "Respond with the message text only -- no preamble, no subject line, no "
    "quotes."
)

SETTLED_THREAD_INSTRUCTION = (
    "Respond with the message text only -- no preamble, no subject line, no "
    f"quotes -- or exactly {NO_REPLY_NEEDED} if the enquirer's latest message "
    "closes the exchange and nothing is left open."
)


def draft_prompt(
    thread_text: str,
    guidance: dict[str, Any],
    reply_required: bool,
) -> str:
    """The single drafting instruction.

    The thread and the guidance travel *in* the prompt rather than through a
    tool, because there is no enquiry tool and there must not be one: a tool
    that let a model name an enquiry would let it name any enquiry. The
    application decided which thread this is; the model only writes the words.
    """
    return (
        "You are drafting one reply to a booking enquiry for Priyanka Homes. "
        "Follow the reply guidance below exactly -- especially "
        "enquiry_drafting_policy, authority_order, conversation_state, "
        "how_to_read_the_conversation and never_invent. "
        "Answer only what the enquirer actually asked; never introduce a topic "
        "they did not raise, and never state a property fact the guidance does "
        "not support -- say you will check instead.\n\n"
        "Do NOT send anything and do not call any tool: everything you need is "
        "here.\n\n"
        f"ENQUIRY THREAD (oldest first):\n{thread_text}\n\n"
        f"REPLY GUIDANCE (JSON):\n{json.dumps(guidance, default=str)}\n\n"
        f"{OPEN_THREAD_INSTRUCTION if reply_required else SETTLED_THREAD_INSTRUCTION}"
    )


class EnquiryReplyService:
    """Turns one press of Generate reply into one draft. Stores nothing."""

    def __init__(
        self,
        enquiries: LodgifyEnquiries,
        agent: AgentService,
        knowledge: KnowledgeStore | None = None,
    ) -> None:
        self._enquiries = enquiries
        self._agent = agent
        self._knowledge = knowledge

    def approved_knowledge(self, property_slug: str | None) -> list[dict[str, Any]]:
        """Owner-approved rules for this property, plus global ones.

        Only APPROVED rows are readable here, as everywhere else: a PROPOSED
        candidate is a suggestion awaiting a human. Knowledge is enrichment, so
        a failure to read it degrades the draft rather than failing the press.
        """
        if self._knowledge is None:
            return []

        try:
            return [
                item.for_drafting()
                for item in self._knowledge.approved_for(property_slug)
            ]

        except Exception:  # noqa: BLE001 -- knowledge is enrichment, not a dependency
            return []

    def draft(
        self,
        enquiry_ref: str,
        actor_user_id: str | None = None,
    ) -> EnquiryReplyDraft:
        """Read the thread, draft a reply, return it. Persist nothing.

        Raises:
            ValueError: the ref names no open enquiry. That is a 404 rather
                than a drafting outcome, so it is the one failure that leaves
                this method as an exception.
        """
        try:
            enquiry, messages = self._enquiries.read_thread(enquiry_ref)

        except (LodgifyConfigurationError, LodgifyUnavailable) as exc:
            # The provider's own words are never forwarded, and a thread we
            # could not read never becomes a reply we invented.
            logger.warning(
                "enquiry thread could not be read for %s: %s",
                enquiry_ref,
                type(exc).__name__,
            )

            return EnquiryReplyDraft(
                enquiry_ref=enquiry_ref,
                subject=None,
                message=None,
                detail=UNREADABLE_DETAIL,
            )

        if not messages:
            return EnquiryReplyDraft(
                enquiry_ref=enquiry_ref,
                subject=None,
                message=None,
                detail=UNREADABLE_DETAIL,
            )

        rows = [message.to_dict() for message in messages]

        guidance = enquiry_reply_guidance(
            rows,
            self.approved_knowledge(enquiry.property_slug),
        )

        # Read off the deterministic analyser, not decided here and not left to
        # the model: this is what makes an open question un-silenceable.
        state = guidance.get("conversation_state") or {}

        reply_required = state.get("suggested_outcome") == REPLY_NEEDED

        try:
            outcome = self._agent.run(
                draft_prompt(render_thread(messages), guidance, reply_required),
                actor_user_id=actor_user_id,
            )

        except Exception as exc:  # noqa: BLE001 -- a failure must never be a fabrication
            logger.warning(
                "enquiry drafting failed for %s: %s",
                enquiry_ref,
                type(exc).__name__,
            )

            return EnquiryReplyDraft(
                enquiry_ref=enquiry_ref,
                subject=None,
                message=None,
                detail=MODEL_FAILED_DETAIL,
            )

        answer = (outcome.get("answer") or "").strip()

        if answer.rstrip(".").strip() == NO_REPLY_NEEDED:
            # The analyser decides *whether* a reply is owed; the model decides
            # only the wording. When they disagree the analyser wins, and the
            # operator is told a reply is owed rather than being handed the
            # "nothing is open" line, which would be false here. Nothing is
            # invented to fill the gap: an honest "write this one yourself"
            # beats a fabricated draft on a path where a person is the gate.
            return EnquiryReplyDraft(
                enquiry_ref=enquiry_ref,
                subject=None,
                message=None,
                detail=DECLINED_DETAIL if reply_required else NOTHING_TO_SAY_DETAIL,
            )

        if not answer or outcome.get("status") != "COMPLETED":
            return EnquiryReplyDraft(
                enquiry_ref=enquiry_ref,
                subject=None,
                message=None,
                detail=MODEL_FAILED_DETAIL,
            )

        return EnquiryReplyDraft(
            enquiry_ref=enquiry.enquiry_ref,
            subject=DEFAULT_SUBJECT,
            message=answer,
            detail=DRAFTED_DETAIL,
        )
