"""What a distilled rule is *about*, and who it is *for*.

Two classifications, both derived from the candidate's own words rather than
from the guest question that led to it. The first live run showed why that
matters: topics were inherited from the historical group, so a rule reading "we
can usually get guests in as early as 10 am" was filed under `amenities`, a
parking rule under `location`, and refund rules under `availability`. Topic is
load-bearing -- the conflict detector and the per-property read both key on it --
so a mis-filed rule is a rule that will not be matched when it should be.

Keyword sets rather than a classifier: deterministic, testable, and cheap to
correct when it gets one wrong.
"""

import re

# Ordered most specific first. "We can get guests in early" is about early
# check-in, not about the check-in process generally, so `early_check_in` has to
# be tested before `checkin_process` or the more general bucket swallows it.
TOPIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "early_check_in",
        (
            "early check-in",
            "early check in",
            "check in early",
            "check-in early",
            "arrive early",
            "get guests in early",
            "get you in early",
            "in as early as",
            "earlier check",
            "early arrival",
        ),
    ),
    (
        "late_checkout",
        (
            "late checkout",
            "late check-out",
            "later checkout",
            "check out late",
            "leave later",
            "stay later",
        ),
    ),
    (
        "refund",
        ("refund", "refunded", "refunding", "money back", "reimburse"),
    ),
    (
        "cancellation",
        ("cancel", "cancelled", "cancellation", "cancelling"),
    ),
    (
        "payment",
        (
            "payment",
            "pay ",
            "paid",
            "card",
            "invoice",
            "deposit",
            "charge",
            "billing",
            "payment link",
            "discount",
        ),
    ),
    (
        "parking",
        ("parking", "park ", "driveway", "garage", "vehicle", "car "),
    ),
    (
        "wifi",
        ("wifi", "wi-fi", "internet", "network"),
    ),
    (
        "location",
        (
            "airport",
            "train",
            "subway",
            "bus",
            "transit",
            "walk from",
            "drive from",
            "miles",
            "neighborhood",
            "neighbourhood",
            "directions",
            "located",
        ),
    ),
    (
        "amenities",
        (
            "towel",
            "linen",
            "kitchen",
            "coffee",
            "opener",
            "washer",
            "dryer",
            "crib",
            "air conditioning",
            "heating",
            "microwave",
            "dishwasher",
            "bed",
        ),
    ),
    (
        "occupancy",
        ("extra guest", "additional guest", "how many people", "occupancy", "pets"),
    ),
    (
        "minimum_stay",
        ("one-night", "one night", "two-night", "minimum stay", "short stays"),
    ),
    (
        "checkout_process",
        ("checkout", "check out", "check-out", "leaving", "trash"),
    ),
    (
        "checkin_process",
        ("check-in", "check in", "checkin", "key", "lockbox", "access", "arrival"),
    ),
    (
        "availability",
        ("availability", "available", "calendar", "booking", "dates", "book"),
    ),
)

DEFAULT_TOPIC = "general"


# Language that addresses colleagues rather than guests, or describes how the
# business runs itself. A guest never needs to know which record the owner
# treats as authoritative.
INTERNAL_MARKERS: tuple[str, ...] = (
    "staff should",
    "staff can",
    "the team",
    "source of truth",
    "internal",
    "my own system",
    "owner's system",
    "owner’s system",
    "the owner may",
    "the owner will",
    "i usually handle",
    "we handle",
    "reconcile",
    "re-run",
    "rerun",
    "reprocess",
    "manually confirmed",
    "workaround",
    "keep the guest informed",
    "set expectations",
    "treat the booking",
    "on our side",
    "in our system",
)

# Positive evidence that a rule is something you would actually say to a guest:
# it describes the property, or what a guest may do.
GUEST_FACING_MARKERS: tuple[str, ...] = (
    "guests can",
    "guests may",
    "guests should",
    "guest can",
    "you can",
    "you may",
    "the property",
    "the home",
    "the house",
    "the apartment",
    "the unit",
    "parking is",
    "parking may",
    "check-in",
    "check in",
    "checkout",
    "wifi",
    "we can",
    "we do not",
    "does not accept",
    "is shared",
    "is available",
    "is not guaranteed",
    "depends on",
    "is applied",
    "is processed",
    "is served by",
)

GUEST_FACING = "GUEST_FACING"

INTERNAL_OPERATION = "INTERNAL_OPERATION"


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s'’-]", " ", (text or "").lower()).split())


def contains(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def derive_topic(title: str, content: str, fallback: str | None = None) -> str:
    """The topic a rule is actually about.

    Read from the candidate's own title and content. The historical group's
    topic is supporting context only -- it describes the question that prompted
    the rule, which is often not what the rule ended up saying.
    """
    text = normalise(f"{title} {content}")

    for topic, markers in TOPIC_RULES:
        if contains(text, markers):
            return topic

    if fallback:
        return fallback

    return DEFAULT_TOPIC


def classify_audience(title: str, content: str) -> str:
    """Whether a rule may be said to a guest.

    Conservative by construction: internal language wins outright, and a rule
    showing no positive sign of being guest-facing is treated as internal.
    Withholding a usable rule costs one draft that says "I'll check"; putting
    the owner's internal booking procedure in front of a guest is a different
    kind of mistake.
    """
    text = normalise(f"{title} {content}")

    if contains(text, INTERNAL_MARKERS):
        return INTERNAL_OPERATION

    if contains(text, GUEST_FACING_MARKERS):
        return GUEST_FACING

    return INTERNAL_OPERATION
