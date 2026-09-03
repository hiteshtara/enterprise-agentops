"""The one place a conversation gets processed.

A webhook and a poll are two ways of learning the same thing -- *something may
have changed* -- so they converge here rather than each growing their own idea
of what to do about it. Webhook is the fast path, polling is the recovery path,
and the fingerprint is what makes them safe to run at the same time.

    fetch authoritative conversation
        -> fingerprint it
        -> already processed this exact state? stop, cost nothing
        -> analyse
             no_reply_needed / already_replied -> record it, no model call
             reply_needed                      -> draft it through the agent
             anything failed                   -> NEEDS_HUMAN_REVIEW, never silence

Two invariants worth stating plainly:

  * **Nothing here sends.** A refresh prepares text and stops. Sending remains
    the DANGEROUS tool behind human approval, unchanged.
  * **One conversation state costs at most one model call.** Four duplicate
    webhook deliveries and a poll all compute the same fingerprint, and the
    first one through does the work.
"""

import logging
from dataclasses import dataclass
from typing import Any

from app.agent import AgentService
from app.cancellation import draft_violates_policy
from app.cancellation import outcome_for as cancellation_outcome
from app.connectors.lodgify.errors import LodgifyUnavailable
from app.connectors.lodgify.inbox import LodgifyInbox
from app.drafts import (
    ConversationDraft,
    DraftStatus,
    DraftStore,
    conversation_fingerprint,
)
from app.early_check_in import outcome_for as early_check_in_outcome
from app.hospitality import analyse_conversation
from app.late_checkout import PROMISE_REASON, exceeds_ceiling
from app.stay_extension import ESCALATION_REASON as STAY_EXTENSION_ESCALATION

logger = logging.getLogger(__name__)

# The deterministic verdicts from app.hospitality.analyse_conversation.
REPLY_NEEDED = "reply_needed"

NO_REPLY_NEEDED = "no_reply_needed"

ALREADY_REPLIED = "already_replied"

DEFAULT_SUBJECT = "Re: your message"

# The token the drafting prompt uses when the model decides silence is right.
NO_REPLY_SENTINEL = "NO_REPLY_NEEDED"

NO_REPLY_DETAIL = (
    "The guest's last message closes the conversation and nothing is open, so "
    "no reply was prepared."
)

ALREADY_REPLIED_DETAIL = (
    "The most recent message in this conversation is ours, so there is nothing "
    "waiting on a reply."
)

MODEL_FAILED_DETAIL = (
    "A reply could not be prepared automatically. Use Regenerate, or write one by hand."
)

ESCALATION_DETAIL = (
    "This reply is prepared but needs your decision before it goes out. Read it, "
    "edit it if you want, then send it for approval."
)

EMPTY_DRAFT_DETAIL = (
    "The model returned nothing usable for this conversation. Use Regenerate, "
    "or write a reply by hand."
)


def draft_prompt(conversation_ref: str) -> str:
    """The drafting instruction.

    Deliberately identical in intent to the console's manual Generate Draft:
    one prompt implementation, so a proactive draft and a hand-triggered one
    cannot drift into being different products. The rules themselves live in
    the hospitality knowledge layer and arrive with the conversation.
    """
    return (
        f"Read guest conversation {conversation_ref} with get_guest_conversation, "
        f"then follow the reply_guidance it returns exactly -- especially "
        f"authority_order, conversation_state and how_to_read_the_conversation. "
        f"Reply only to what is still open; never re-answer something already "
        f"answered. Do NOT send anything. Respond with the message text only -- "
        f"no preamble, no subject line, no quotes -- or exactly "
        f"{NO_REPLY_SENTINEL} if no message is worth sending."
    )


def is_no_reply(answer: str) -> bool:
    return answer.strip().rstrip(".").strip() == NO_REPLY_SENTINEL


@dataclass(frozen=True)
class RefreshResult:
    """What one refresh did. Counts and status only -- never guest text."""

    conversation_ref: str
    fingerprint: str | None
    status: str | None
    created: bool
    model_called: bool
    skipped: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_ref": self.conversation_ref,
            "status": self.status,
            "created": self.created,
            "model_called": self.model_called,
            "skipped": self.skipped,
            "detail": self.detail,
        }


class ConversationRefreshService:
    """Prepares whatever a conversation needs, exactly once per state."""

    def __init__(
        self,
        inbox: LodgifyInbox,
        drafts: DraftStore,
        agent: AgentService,
    ) -> None:
        self._inbox = inbox
        self._drafts = drafts
        self._agent = agent

    def process(
        self,
        conversation_ref: str,
        force: bool = False,
        actor_user_id: str | None = None,
    ) -> RefreshResult:
        """Bring one conversation's prepared state up to date.

        `force` is the console's Regenerate: it redoes the work for the current
        state even though it is already settled. Nothing automatic ever sets it,
        so a poll can never spend a second model call on an unchanged thread.
        """
        try:
            conversation = self._inbox.get_conversation(conversation_ref)

        except (LodgifyUnavailable, ValueError) as exc:
            # Keep whatever good state already exists. A provider hiccup must
            # not erase a perfectly good draft or invent a conversation.
            logger.warning(
                "conversation refresh could not read %s: %s",
                conversation_ref,
                type(exc).__name__,
            )

            return RefreshResult(
                conversation_ref=conversation_ref,
                fingerprint=None,
                status=None,
                created=False,
                model_called=False,
                skipped=True,
                detail="The conversation could not be read; previous state kept.",
            )

        messages = conversation.get("messages") or []
        fingerprint = conversation_fingerprint(messages)
        property_slug = conversation.get("property_slug")

        existing = self._drafts.for_state(conversation_ref, fingerprint)

        if existing is not None and existing.is_settled() and not force:
            # The idempotency boundary. This is what makes four duplicate
            # webhook deliveries cost one model call between them.
            return RefreshResult(
                conversation_ref=conversation_ref,
                fingerprint=fingerprint,
                status=existing.status,
                created=False,
                model_called=False,
                skipped=True,
                detail="Already processed for this conversation state.",
            )

        # Straight from the deterministic analyser rather than from a tool
        # result: this decides whether a model call happens at all, so it must
        # not depend on the shape of something the tool layer assembles.
        analysis = analyse_conversation(messages)
        verdict = analysis.get("suggested_outcome")

        if verdict == NO_REPLY_NEEDED and not force:
            # A deterministic closing costs nothing automatically. `force` is the
            # one way past this, and only a person pressing Regenerate sets it:
            # the rule is advice, and a human asking for a draft outranks it. The
            # model still gets the same analysis and may answer NO_REPLY_NEEDED
            # itself -- which is the conservative direction, since an unnecessary
            # draft is cheaper than a wrongly-withheld one.
            return self._record(
                conversation_ref,
                fingerprint,
                property_slug,
                DraftStatus.NO_REPLY_NEEDED,
                detail=NO_REPLY_DETAIL,
            )

        if verdict != REPLY_NEEDED and verdict != NO_REPLY_NEEDED:
            # `already_replied` lands here, and so does anything unrecognised.
            # `force` deliberately does *not* reach past this one: the newest
            # message being ours is what stops a successful send from triggering
            # a reply to itself, and no button may switch that off.
            return self._record(
                conversation_ref,
                fingerprint,
                property_slug,
                DraftStatus.NO_REPLY_NEEDED,
                detail=ALREADY_REPLIED_DETAIL,
            )

        escalate = bool(analysis.get("owner_approval_required"))
        reason = analysis.get("owner_approval_reason")

        if analysis.get("early_check_in_requested"):
            # Deciding whether early access is possible is the owner's, not
            # ours, in every branch except the one that says no. Unknown
            # escalates too: an unreachable provider must never read as "nobody
            # is checking out".
            _verdict, why, needs_owner = early_check_in_outcome(
                self._same_day_checkout(conversation_ref)
            )

            if needs_owner:
                escalate = True
                reason = why

        if analysis.get("stay_extension_requested"):
            # Detected and handed straight to the owner. Deliberately no
            # availability read, no booking-overlap scan and no second model
            # call to pin down dates: whether nights can be sold, and whether
            # the owner wants to sell them, is not something to compute. Gated
            # on an *open* request, like every other topic, so a request the
            # owner already answered cannot escalate twice.
            escalate = True
            reason = STAY_EXTENSION_ESCALATION

        # A cancellation the guest is asking about the mechanics of, or taking
        # up, is a person's decision -- AgentGuard has no tool that changes a
        # price or restores a reservation, and must not imply otherwise.
        _offer, cancellation_escalates, cancellation_reason = cancellation_outcome(
            conversation.get("booking_cancelled"),
            messages,
        )

        if cancellation_escalates:
            escalate = True
            reason = cancellation_reason

        return self._draft(
            conversation_ref,
            fingerprint,
            property_slug,
            actor_user_id,
            force,
            escalate=escalate,
            escalation_reason=reason,
        )

    def _same_day_checkout(self, conversation_ref: str) -> bool | None:
        """Whether a stay ends on this guest's arrival day, or None if unknown.

        Every failure resolves to None, which escalates. Nothing here may turn
        an outage into an answer.
        """
        try:
            turnover = self._inbox.turnover_for(conversation_ref)

        except Exception as exc:  # noqa: BLE001 -- unknown is the safe answer
            logger.warning(
                "turnover lookup failed for %s: %s",
                conversation_ref,
                type(exc).__name__,
            )

            return None

        value = turnover.get("same_day_checkout")

        return value if isinstance(value, bool) else None

    def _draft(
        self,
        conversation_ref: str,
        fingerprint: str,
        property_slug: str | None,
        actor_user_id: str | None,
        force: bool,
        escalate: bool = False,
        escalation_reason: str | None = None,
    ) -> RefreshResult:
        """Write a reply through the ordinary agent run.

        Through the agent rather than a bare model call, so drafting is a Run
        like any other: audited, measured, and traceable from the draft back to
        the model call that produced it.
        """
        try:
            outcome = self._agent.run(
                draft_prompt(conversation_ref),
                actor_user_id=actor_user_id,
            )

        except Exception as exc:  # noqa: BLE001 -- a failure must never be silence
            logger.warning(
                "drafting failed for %s: %s", conversation_ref, type(exc).__name__
            )

            return self._record(
                conversation_ref,
                fingerprint,
                property_slug,
                DraftStatus.NEEDS_HUMAN_REVIEW,
                detail=MODEL_FAILED_DETAIL,
                model_called=True,
                force=force,
            )

        answer = (outcome.get("answer") or "").strip()
        run_id = outcome.get("run_id")

        if is_no_reply(answer):
            return self._record(
                conversation_ref,
                fingerprint,
                property_slug,
                DraftStatus.NO_REPLY_NEEDED,
                detail=NO_REPLY_DETAIL,
                source_run_id=run_id,
                model_called=True,
                force=force,
            )

        if not answer or outcome.get("status") != "COMPLETED":
            return self._record(
                conversation_ref,
                fingerprint,
                property_slug,
                DraftStatus.NEEDS_HUMAN_REVIEW,
                detail=EMPTY_DRAFT_DETAIL,
                source_run_id=run_id,
                model_called=True,
                force=force,
            )

        # Policy is enforced on the text, not merely asked for in the prompt.
        # The guidance tells the model the ceiling; this is what happens when it
        # ignores it, and it is the reason a promise past 11:00 AM cannot reach
        # a guest as an ordinary ready-to-send draft.
        violation = draft_violates_policy(answer)

        if escalate or exceeds_ceiling(answer) or violation:
            return self._record(
                conversation_ref,
                fingerprint,
                property_slug,
                DraftStatus.NEEDS_HUMAN_REVIEW,
                subject=DEFAULT_SUBJECT,
                message=answer,
                detail=(
                    escalation_reason
                    if escalate and escalation_reason
                    else (violation or PROMISE_REASON)
                )
                + " "
                + ESCALATION_DETAIL,
                source_run_id=run_id,
                model_called=True,
                force=force,
            )

        return self._record(
            conversation_ref,
            fingerprint,
            property_slug,
            DraftStatus.DRAFT_READY,
            subject=DEFAULT_SUBJECT,
            message=answer,
            source_run_id=run_id,
            model_called=True,
            force=force,
        )

    def _record(
        self,
        conversation_ref: str,
        fingerprint: str,
        property_slug: str | None,
        status: DraftStatus,
        subject: str | None = None,
        message: str | None = None,
        detail: str | None = None,
        source_run_id: str | None = None,
        model_called: bool = False,
        force: bool = False,
    ) -> RefreshResult:
        draft, created = self._drafts.record_outcome(
            conversation_ref=conversation_ref,
            conversation_fingerprint=fingerprint,
            status=status,
            property_slug=property_slug,
            subject=subject,
            message=message,
            detail=detail,
            source_run_id=source_run_id,
        )

        if force and not created:
            # Regenerate replaces the settled outcome the operator rejected.
            draft = self._drafts.replace(
                draft.draft_ref,
                status=status,
                subject=subject,
                message=message,
                detail=detail,
                source_run_id=source_run_id,
            )

        return RefreshResult(
            conversation_ref=conversation_ref,
            fingerprint=fingerprint,
            status=draft.status,
            created=created,
            model_called=model_called,
            skipped=False,
            detail=detail or "",
        )

    def current(self, conversation_ref: str) -> ConversationDraft | None:
        return self._drafts.current_for(conversation_ref)
