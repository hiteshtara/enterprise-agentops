"""Owner-authored late-checkout policy, enforced deterministically.

The facts are the owner's, supplied directly rather than distilled from past
messages:

    standard checkout               10:00 AM
    automatic late-checkout ceiling 11:00 AM
    anything later than 11:00 AM    needs the owner's approval

The wording of a reply belongs in the hospitality knowledge layer, where the
owner can edit it. What lives here is the part that must not depend on a model
agreeing: reading a time out of a guest's request, reading a time out of a draft
we are about to put in front of someone, and comparing both against the ceiling.

The direction of every uncertainty is deliberate. A time we cannot parse, or an
hour with no am/pm in a checkout sentence, resolves *towards* escalation --
offering an hour we should not have offered is a promise the business has to
keep, while an unnecessary escalation costs the owner a glance.
"""

import re
from typing import Any

STANDARD_CHECKOUT = "10:00 AM"

AUTOMATIC_CEILING = "11:00 AM"

# Minutes past midnight. 11:00 is the latest AgentGuard may offer or confirm on
# its own; a request past it is the owner's decision, not ours.
CEILING_MINUTES = 11 * 60

# Words that make a sentence about leaving rather than arriving. Check-in is
# excluded explicitly further down: "check-in is from 3 PM" is not a promise of
# a 3 PM checkout, and must never be read as one.
CHECKOUT_MARKERS: tuple[str, ...] = (
    "checkout",
    "check out",
    "check-out",
    "checking out",
    "leave",
    "leaving",
    "stay until",
    "stay till",
    "stay til",
    "stay longer",
    "stay a bit",
    "stay a little",
    "out of the",
    "vacate",
)

ARRIVAL_MARKERS: tuple[str, ...] = (
    "check-in",
    "check in",
    "checkin",
    "checking in",
    "arrive",
    "arrival",
    "arriving",
    "get in",
)

# A request to stay on with no time attached at all: "can we have a late
# checkout?". Within policy -- the answer is the one-hour offer.
LATE_CHECKOUT_MARKERS: tuple[str, ...] = (
    "late checkout",
    "late check out",
    "late check-out",
    "later checkout",
    "later check out",
    "later check-out",
    "check out later",
    "checkout later",
    "stay later",
    "stay a little later",
    "stay a bit later",
    "stay longer",
    "leave later",
    "extend checkout",
    "extend our checkout",
    "extended checkout",
    "later departure",
)

NOON_WORDS: tuple[str, ...] = ("noon", "midday", "mid-day", "midi")

# 1 pm / 1:30pm / 13:00 / 11 / half past 11.
TIME_PATTERN = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'clock)?",
    re.IGNORECASE,
)

SENTENCE_SPLIT = re.compile(r"[.!?;\n]+")

ESCALATION_REASON = (
    "The guest asked to check out later than the 11:00 AM that AgentGuard may "
    "offer on its own. The prepared reply offers 11:00 AM and says anything "
    "later has to be checked -- confirming the later time is the owner's "
    "decision."
)

PROMISE_REASON = (
    "The prepared reply names a checkout time later than 11:00 AM, which is "
    "past what AgentGuard may offer without the owner's approval."
)

# What the model is told, so a draft written under this policy says the right
# thing the first time rather than being caught by the check afterwards.
POLICY_GUIDANCE = (
    f"Standard checkout is {STANDARD_CHECKOUT}. When a guest asks about "
    f"checking out later, offer {AUTOMATIC_CEILING} -- that extra hour is "
    f"already approved and does not need checking. {AUTOMATIC_CEILING} is also "
    f"the latest you may ever offer or confirm. If the guest asks for a later "
    f"time than that, do not refuse and do not promise it: offer "
    f"{AUTOMATIC_CEILING} as what is available now, and say plainly that "
    f"anything later has to be checked first. Never volunteer a longer "
    f"extension than the guest asked for."
)


def normalise(text: object) -> str:
    if not isinstance(text, str):
        return ""

    return " ".join(text.lower().replace("’", "'").split())


def mentions_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def to_minutes(hour: int, minute: int, meridiem: str | None) -> int | None:
    """One clock reading in minutes past midnight, or None if it is not a time.

    A bare hour in a checkout sentence is resolved the way a guest means it:
    10, 11 and 12 are the morning hours anyone discusses checkout in, and 1
    through 9 mean the afternoon. That reading is also the cautious one, since
    it puts an ambiguous "can we stay until 1?" past the ceiling.
    """
    if not 0 <= minute < 60:
        return None

    if meridiem:
        flag = meridiem.replace(".", "").lower()

        if not 1 <= hour <= 12:
            return None

        if flag.startswith("p"):
            return (hour % 12 + 12) * 60 + minute

        if flag.startswith("a"):
            return (hour % 12) * 60 + minute

        # "11 o'clock" -- no meridiem, fall through to the bare-hour reading.

    if hour == 12:
        return 12 * 60 + minute

    if 10 <= hour <= 11:
        return hour * 60 + minute

    if 1 <= hour <= 9:
        return (hour + 12) * 60 + minute

    if 13 <= hour <= 23:
        return hour * 60 + minute

    return None


def checkout_sentences(text: str) -> list[str]:
    """The parts of a message that are about leaving, not arriving."""
    sentences = [part.strip() for part in SENTENCE_SPLIT.split(text) if part.strip()]

    return [
        sentence
        for sentence in sentences
        if mentions_any(sentence, CHECKOUT_MARKERS)
        and not mentions_any(sentence, ARRIVAL_MARKERS)
    ]


def latest_checkout_time(text: object) -> int | None:
    """The latest checkout time named anywhere in a message, in minutes.

    The *latest* rather than the first, because a sentence that mentions both
    the standard time and a requested one -- "checkout is 10, can we make it
    1?" -- is asking about the later of the two.
    """
    normalised = normalise(text)

    if not normalised:
        return None

    times: list[int] = []

    for sentence in checkout_sentences(normalised):
        if mentions_any(sentence, NOON_WORDS):
            times.append(12 * 60)

        for hour, minute, meridiem in TIME_PATTERN.findall(sentence):
            parsed = to_minutes(int(hour), int(minute or 0), meridiem or None)

            if parsed is not None:
                times.append(parsed)

    return max(times) if times else None


def is_late_checkout_request(text: object) -> bool:
    """Whether a guest message asks to stay past the standard checkout."""
    normalised = normalise(text)

    if not normalised:
        return False

    if mentions_any(normalised, LATE_CHECKOUT_MARKERS):
        return True

    named = latest_checkout_time(normalised)

    return named is not None and named > 10 * 60


def exceeds_ceiling(text: object) -> bool:
    """Whether a message asks for, or promises, a checkout past 11:00 AM."""
    named = latest_checkout_time(text)

    return named is not None and named > CEILING_MINUTES


def requires_owner_approval(messages: Any) -> bool:
    """Whether anything still open asks to check out past the ceiling.

    Takes the guest messages that arrived after our last reply, so a request
    the owner already answered does not escalate the thread forever.
    """
    return any(exceeds_ceiling(row.get("message")) for row in _rows(messages))


def _rows(messages: Any) -> list[dict[str, Any]]:
    return [row for row in (messages or []) if isinstance(row, dict)]
