"""Owner-authored cancellation-retention policy, enforced deterministically.

When a guest actually cancels, Priyanka Homes would rather keep the business:

    30% off       if they keep or restore the current reservation
    30% off       toward a next Boston visit if they cancel anyway

Two things make this policy dangerous to implement loosely, and both are handled
here rather than in a prompt.

**The trigger is a fact, not a mood.** A guest who asks about the cancellation
policy, mentions they *might* cancel, asks what refund they would get, or wants
to shorten their stay has not cancelled. Offering a discount to any of them
would turn "what's your cancellation policy?" into a 30% giveaway. The trigger
is authoritative booking state, and message text alone never fires it.

**The mechanics do not exist yet.** The owner specified a number and nothing
else -- no coupon code, no expiry, no blackout dates, no answer on whether taxes
and channel fees are included, no stacking rule, and no way to redeem the future
discount. A model asked about any of those will invent a plausible answer, and
an invented discount term is a promise the business then has to honour. Every
one of those questions escalates instead.

AgentGuard also cannot act on the offer even when it is accepted: it has no tool
that changes a price or restores a reservation, and acceptance escalates.
"""

from typing import Any

RETENTION_DISCOUNT = "30%"

# Booking statuses that mean the reservation is off. Both spellings are
# accepted: Lodgify's own field is `canceled_at`, and the status vocabulary
# observed live in this account is Booked / Declined / Open -- no cancelled
# booking has ever been seen, so the exact string is defensive rather than
# verified. `canceled_at` is the primary signal for that reason.
CANCELLED_STATUSES: tuple[str, ...] = ("cancelled", "canceled")

# Wording that sounds like cancellation but is not one. Listed to be explicit
# about what must NOT trigger the offer; the trigger reads booking state, so
# these are never consulted for it.
NOT_A_CANCELLATION: tuple[str, ...] = (
    "cancellation policy",
    "might cancel",
    "may cancel",
    "thinking of cancelling",
    "thinking of canceling",
    "if i cancel",
    "if we cancel",
    "what happens if",
    "refund",
)

# Details the owner has not specified. Answering any of them means inventing a
# term the business would then have to honour.
UNSPECIFIED_MECHANICS: tuple[str, ...] = (
    "coupon",
    "promo code",
    "promotion code",
    "discount code",
    "voucher",
    "expire",
    "expiry",
    "expiration",
    "valid until",
    "valid for",
    "blackout",
    "black-out",
    "stack",
    "combine",
    "taxes",
    "tax",
    "fees",
    "service charge",
    "cleaning fee",
    "redeem",
    "how do i use",
    "how do we use",
    "how would i use",
    "apply the discount",
    "apply it",
)

# Wording that reads as taking the offer up.
ACCEPTANCE_MARKERS: tuple[str, ...] = (
    "yes please",
    "we'll take it",
    "we will take it",
    "i'll take it",
    "we accept",
    "i accept",
    "sounds good",
    "let's do that",
    "lets do that",
    "keep the reservation",
    "keep our reservation",
    "keep my reservation",
    "restore",
    "reinstate",
    "rebook",
    "put it back",
    "we'd like the 30",
    "we would like the 30",
    "take the 30",
    "yes to the 30",
)

POLICY_GUIDANCE = (
    f"If the reservation has actually been cancelled, say that Priyanka Homes "
    f"values their business and would like to keep it: offer {RETENTION_DISCOUNT} "
    f"off if they keep or restore the current reservation, and say the same "
    f"{RETENTION_DISCOUNT} can be applied to their next Boston visit if they "
    f"still need to cancel. Keep it warm and brief. The discount is "
    f"{RETENTION_DISCOUNT} and never any other number, whatever a past reply "
    f"says. Never make this offer to a guest who has only asked about the "
    f"cancellation policy, mentioned they might cancel, asked about a refund, or "
    f"asked to change or shorten their dates -- none of those is a cancellation. "
    f"Do not make the offer twice: if it has already been made in this thread, "
    f"do not repeat it. And never invent how the discount works -- there is no "
    f"coupon code, expiry date, blackout list, stacking rule, or decision about "
    f"taxes and channel fees to give out, and you cannot apply it yourself."
)

TRIGGER_REASON = (
    "The reservation is cancelled, so the retention offer applies. Nothing "
    "about the discount is applied automatically -- sending the reply is the "
    "only thing this does."
)

MECHANICS_REASON = (
    f"The guest is asking how the {RETENTION_DISCOUNT} offer actually works. The "
    f"owner has not specified a coupon, an expiry, blackout dates, stacking, or "
    f"how taxes and channel fees are treated, so this needs a person rather than "
    f"an invented answer."
)

ACCEPTANCE_REASON = (
    f"The guest is taking up the {RETENTION_DISCOUNT} offer. AgentGuard cannot "
    f"change a price or restore a reservation, so applying it is a person's job."
)

WRONG_DISCOUNT_REASON = (
    f"The prepared reply names a discount other than {RETENTION_DISCOUNT}, which "
    f"is the only figure the owner approved."
)

INVENTED_MECHANICS_REASON = (
    "The prepared reply describes how the discount works -- a code, an expiry, "
    "blackout dates, stacking, or fee treatment. None of that has been decided, "
    "so it cannot go to a guest unread."
)


def normalise(text: object) -> str:
    if not isinstance(text, str):
        return ""

    return " ".join(text.lower().replace("’", "'").split())


def mentions_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def is_cancelled(booking_status: object, canceled_at: object = None) -> bool | None:
    """Whether the reservation is off, or None when booking state is unknown.

    Three-valued on purpose. `None` means we could not establish the booking
    state, and it must never behave like `False`-with-confidence *or* trigger
    the offer: a discount handed out because a status could not be read is as
    wrong as one withheld.
    """
    if isinstance(canceled_at, str) and canceled_at.strip():
        return True

    if not isinstance(booking_status, str) or not booking_status.strip():
        return None

    return booking_status.strip().lower() in CANCELLED_STATUSES


def asks_about_mechanics(text: object) -> bool:
    """Whether a message asks about a discount term nobody has decided."""
    return mentions_any(normalise(text), UNSPECIFIED_MECHANICS)


def accepts_offer(text: object) -> bool:
    """Whether a message reads as taking the retention offer up."""
    return mentions_any(normalise(text), ACCEPTANCE_MARKERS)


def offer_already_made(messages: Any) -> bool:
    """Whether the retention offer has already gone out in this thread.

    Read from our own messages, which is what makes "do not double-offer" a
    property of the conversation rather than of a flag somebody has to maintain.
    """
    return any(
        row.get("sender") == "Owner"
        and RETENTION_DISCOUNT in (row.get("message") or "")
        for row in (messages or [])
        if isinstance(row, dict)
    )


def names_another_discount(text: object) -> bool:
    """Whether a draft names a percentage that is not the approved one.

    A historical reply offering 20% is exactly how a superseded number gets back
    into circulation, so the check is on the text rather than on the prompt.
    """
    normalised = normalise(text)

    if "%" not in normalised:
        return False

    figures = {
        word.rstrip("%")
        for word in normalised.replace("%", "% ").split()
        if word.endswith("%")
    }

    return any(figure and figure != "30" for figure in figures)


def describes_mechanics(text: object) -> bool:
    """Whether a draft explains discount mechanics that do not exist."""
    return mentions_any(normalise(text), UNSPECIFIED_MECHANICS)


def outcome_for(
    cancelled: bool | None,
    messages: Any,
) -> tuple[bool, bool, str | None]:
    """Whether to offer, whether the owner must decide, and why.

    Returns `(offer, escalate, reason)`.
    """
    unanswered = [
        row
        for row in (messages or [])
        if isinstance(row, dict) and row.get("sender") == "Renter"
    ]

    if any(asks_about_mechanics(row.get("message")) for row in unanswered):
        return False, True, MECHANICS_REASON

    if cancelled is not True:
        # Not cancelled, or we could not tell. Either way the offer stays shut.
        return False, False, None

    if any(accepts_offer(row.get("message")) for row in unanswered):
        # Checked before "have we offered yet", not after. A guest taking the
        # offer up needs a person whether or not this thread is where they were
        # told about it -- AgentGuard cannot apply a discount either way.
        return False, True, ACCEPTANCE_REASON

    if offer_already_made(messages):
        return False, False, None

    return True, False, TRIGGER_REASON


def draft_violates_policy(text: object) -> str | None:
    """Why a prepared reply may not go out as an ordinary draft, if it may not."""
    if names_another_discount(text):
        return WRONG_DISCOUNT_REASON

    if describes_mechanics(text):
        return INVENTED_MECHANICS_REASON

    return None
