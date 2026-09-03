"""Stay-extension detection. Recognised, then handed to the owner.

A guest already staying asks to keep the home for another night, to add days,
or to move their dates. AgentGuard does **not** work out whether that is
possible. It deliberately computes nothing about the calendar here: no
availability read, no booking-overlap scan, no window arithmetic, no date
extraction, and no free/occupied/unknown reasoning. There is no verdict in this
module because there is no verdict to reach.

That is a scope decision, not a missing feature. Answering an extension well
means being right about the calendar *and* about what the owner is willing to
sell, and the second half is not a fact a system can look up. Handling the
routine majority reliably is worth more than handling extensions cleverly and
occasionally wrongly, so the whole apparatus was removed rather than tuned.

What is left is the two things escalation needs:

    detection   `is_extension_request` / `requested_in` -- is this open?
    wording     `POLICY_GUIDANCE` -- what may never be said while it is

An open request routes to the same human-review path every other escalation
uses. The prepared reply must promise nothing, quote no availability, and claim
no reservation was changed -- the owner answers it.

The module is pure and performs no I/O.
"""

from typing import Any

# Wording that asks for more nights, rather than for a later hour on the last
# day. "stay longer", "leave later" and "late departure" are deliberately absent:
# they are checkout-time wording, they already belong to the late-checkout
# policy, and reading them as an extension request would answer a question about
# an hour with a question about a night.
EXTENSION_MARKERS: tuple[str, ...] = (
    "extra night",
    "another night",
    "one more night",
    "two more nights",
    "a few more nights",
    "additional night",
    "extra day",
    "more days",
    "extend our stay",
    "extend my stay",
    "extend the stay",
    "extend our reservation",
    "extend my reservation",
    "extend the reservation",
    "extend our booking",
    "extend my booking",
    "extend the booking",
    "extend our trip",
    "extend my trip",
    "extend our dates",
    "extend the dates",
    "can we extend",
    "could we extend",
    "possible to extend",
    "like to extend",
    "want to extend",
    "hoping to extend",
    "extend for",
    "extend by",
    "extend until",
    "extend till",
    "extend through",
    "stay an extra",
    "stay another",
    "stay one more",
    "stay two more",
    "add a night",
    "add another night",
    "add an extra night",
    "keep the place another",
    "keep the home another",
)

# What the model is told while the request is open. The runtime escalates
# regardless of what the model does with this -- the guidance exists so the
# prepared text is one a person can send, not so it can decide anything.
POLICY_GUIDANCE = (
    "A guest asking to add nights, change dates or extend a stay they already "
    "have is asking for something only the owner can decide. AgentGuard does "
    "not check the calendar for this and has not looked anything up. Never say "
    "the stay has been extended, that the nights are added, held, booked or "
    "arranged, and never imply the reservation has changed -- this reply "
    "changes nothing. Never say the nights are available, free, taken or "
    "booked, and never quote availability of any kind: nothing here knows. "
    "Never say who else may be in the home, when they arrive or leave, how "
    "they booked, or what they paid. Do not offer a shorter version of what "
    "was asked for -- a partial stay is a different reservation and the "
    "owner's to sell. Say only that you will check the dates with the owner "
    "and come back to them. This reply is going to a person to finish."
)

# Why an open request parks for a human. Phrased for the owner reading the
# console, like every other escalation reason.
ESCALATION_REASON = (
    "The guest asked to extend their stay, add nights or change their dates. "
    "AgentGuard does not decide that: whether the nights can be sold, and "
    "whether you want to sell them, is yours. The prepared reply promises "
    "nothing and quotes no availability -- read it, edit it, and send it "
    "yourself."
)


def normalise(text: object) -> str:
    if not isinstance(text, str):
        return ""

    return " ".join(text.lower().replace("’", "'").split())


def is_extension_request(text: object) -> bool:
    """Whether a guest message asks for more nights than they have booked."""
    normalised = normalise(text)

    return any(marker in normalised for marker in EXTENSION_MARKERS)


def requested_in(messages: Any) -> bool:
    """Whether anything still open asks to stay additional nights.

    Read over the *unanswered* messages by callers, which is what stops a
    request the owner has already replied to from escalating a second time.
    """
    return any(
        is_extension_request(row.get("message"))
        for row in (messages or [])
        if isinstance(row, dict)
    )
