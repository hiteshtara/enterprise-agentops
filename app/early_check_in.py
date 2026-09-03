"""Owner-authored early check-in policy, enforced deterministically.

The facts are the owner's, supplied directly rather than distilled from past
messages:

    standard check-in   4:00 PM
    early check-in      never promised because a guest asked

What makes this different from late checkout is that the answer depends on a
fact AgentGuard has to *look up* rather than compute: is another guest checking
out of this property on the arrival day? Three states follow, and they are kept
distinct all the way to the screen:

    same-day checkout      -> decline politely; the turnover needs the full day
    no same-day checkout   -> may be possible; the owner decides, not us
    unknown                -> say the schedule needs checking; the owner decides

The third is why `same_day_checkout` is three-valued. A provider outage that
collapsed to `False` would read as "nobody is leaving" and turn an unreachable
API into a promise of early access on a turnover day.

Note what is *not* here: AgentGuard never chooses an early check-in time. The
best outcome it can reach on its own is "may be possible, let me confirm".
"""

from typing import Any

STANDARD_CHECK_IN = "4:00 PM"

EARLY_CHECK_IN_MARKERS: tuple[str, ...] = (
    "early check in",
    "early check-in",
    "early checkin",
    "check in early",
    "check-in early",
    "checkin early",
    "check in earlier",
    "arrive early",
    "arrive earlier",
    "arriving early",
    "get in early",
    "get in earlier",
    "drop our bags",
    "drop off our bags",
    "drop bags",
    "leave our bags",
    "leave our luggage",
    "earlier check in",
    "earlier check-in",
    "early arrival",
    "come early",
)

# What the model is told. The runtime enforces the outcome regardless.
POLICY_GUIDANCE = (
    f"Standard check-in is {STANDARD_CHECK_IN}. Never promise early check-in "
    f"just because a guest asked, and never name an earlier time yourself -- "
    f"choosing the time is the owner's decision, not yours. Whether early "
    f"check-in is possible depends on whether another guest is checking out of "
    f"the property on the arrival day, which is a fact to look up rather than "
    f"guess. If a guest is checking out that day, say plainly that the "
    f"turnover needs the full day and that check-in is at {STANDARD_CHECK_IN}; "
    f"do not blame the cleaners and do not leave the door open to it happening "
    f"anyway. If nobody is checking out, say early check-in may be possible and "
    f"that you will confirm the schedule -- not that it is agreed. If the "
    f"schedule cannot be checked, say exactly that; never turn a system you "
    f"could not reach into 'there is no checkout'."
)

FAR_FUTURE_GUIDANCE = (
    f"The arrival is far enough off that the schedule cannot be settled yet. "
    f"Say that check-in is {STANDARD_CHECK_IN}, that early check-in depends on "
    f"whether a guest is checking out that day, and that you will be able to "
    f"confirm closer to arrival. Do not promise it and do not name a time."
)

DECLINE_REASON = (
    "Another guest checks out of this property on the arrival day, so early "
    "check-in is declined and the reply says check-in is at "
    f"{STANDARD_CHECK_IN}."
)

POSSIBLE_REASON = (
    "Nobody is checking out on the arrival day, so early check-in may be "
    "possible -- but the time is the owner's decision, so the reply only offers "
    "to confirm."
)

UNKNOWN_REASON = (
    "The turnover schedule for the arrival day could not be established, so "
    "the reply says it needs checking rather than guessing either way."
)


def normalise(text: object) -> str:
    if not isinstance(text, str):
        return ""

    return " ".join(text.lower().replace("’", "'").split())


def is_early_check_in_request(text: object) -> bool:
    """Whether a guest message asks to arrive before the standard time."""
    normalised = normalise(text)

    return any(marker in normalised for marker in EARLY_CHECK_IN_MARKERS)


def requested_in(messages: Any) -> bool:
    """Whether anything still open asks about arriving early."""
    return any(
        is_early_check_in_request(row.get("message"))
        for row in (messages or [])
        if isinstance(row, dict)
    )


def outcome_for(same_day_checkout: bool | None) -> tuple[str, str, bool]:
    """The verdict, its reason, and whether the owner must sign the reply off.

    Only one branch is answerable on our own, and it is the one that says no.
    Declining is a statement of existing policy; anything that opens the door to
    early access is a decision about the owner's day.
    """
    if same_day_checkout is True:
        return "declined", DECLINE_REASON, False

    if same_day_checkout is False:
        return "possible", POSSIBLE_REASON, True

    return "unknown", UNKNOWN_REASON, True
