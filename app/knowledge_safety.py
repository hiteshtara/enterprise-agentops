"""What a distilled candidate is not allowed to say.

Distillation asks a model to read a group of real past replies and write down
the rule behind them. That is exactly the operation most likely to launder a
one-off into a policy, a specific stay into a standing promise, or a price from
March into a fact.

So candidates pass through a deterministic filter before they are stored. The
filter is not a quality judgement -- an owner can reject a dull-but-safe rule
themselves. It rejects the things a human reviewer might *approve by accident*
because they read plausibly: a confident price, a door code, a date, a promise
made to somebody in particular.

Rejecting a good candidate costs one rule the owner can write by hand.
Approving a bad one puts a wrong promise in front of every future guest.
"""

import re
from dataclasses import dataclass

# Evidence thresholds. One example is an anecdote; the spec's own instruction is
# "do not infer from one weak example".
MIN_EVIDENCE_FOR_PROPERTY_SCOPE = 2

# Global means "true of every property". Claiming that from one property's
# replies is the single most likely way to generalise a local arrangement into a
# portfolio-wide lie.
MIN_PROPERTIES_FOR_GLOBAL_SCOPE = 2

MIN_CONTENT_WORDS = 4

MAX_CONTENT_WORDS = 90

# Money in any of the forms a reply might carry it. Prices change and a stale
# one is worse than no answer -- pricing has an authoritative tool.
PRICE_PATTERN = re.compile(
    r"(?:[$£€]\s?\d|(?<![a-z])\d+(?:\.\d+)?\s?(?:usd|dollars?|eur|gbp)(?![a-z]))",
    re.IGNORECASE,
)

# Any run of digits long enough to be a code, and anything explicitly called one.
CODE_PATTERN = re.compile(r"\b\d{4,}\b")

CODE_WORDS = (
    "door code",
    "lockbox",
    "lock box",
    "keypad",
    "access code",
    "pin",
    "password",
    "wifi code",
)

# A specific calendar date is a fact about one stay, not a standing rule.
DATE_PATTERN = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2})\b",
    re.IGNORECASE,
)

# Language that ties a rule to one guest or one booking.
BOOKING_SPECIFIC_MARKERS = (
    "your booking",
    "your reservation",
    "your stay",
    "for you",
    "this guest",
    "the guest asked",
    "i told them",
    "i confirmed for",
    "as agreed with",
    "we agreed",
    "i promised",
)

# A rule that guarantees something the business cannot control. Early check-in
# and late checkout depend on the previous stay and on cleaning; a rule that
# promises them is a rule that will be broken.
OVERPROMISE_MARKERS = (
    "always available",
    "is guaranteed",
    "we guarantee",
    "guaranteed to",
    "will always be",
    "can always",
    "never a problem",
    "no need to ask",
)

# Identity leaking through from an example despite upstream redaction.
IDENTITY_MARKERS = ("[redacted]", "@", "http://", "https://")


# -- operational numbers --------------------------------------------------
#
# The first live run produced "in as early as 10 am", "around 2 pm", "about a
# 20-25 minute drive", "1.5 miles away" and "fits two cars" -- and the filter
# passed every one, because it only looked for currency, 4+ digit runs and
# calendar dates.
#
# These are not the same risk as a price and must not be treated as one. They
# are genuinely useful knowledge that happens to be *checkable and perishable*:
# a driveway gets resurfaced, a bus route moves, a cleaner's schedule changes.
# So they are neither passed silently nor thrown away -- they are flagged, and
# an owner confirms the number before it becomes something guests are told.
#
# Rejecting every number would be worse than useless: "the property does not
# accept one-night bookings" is one of the best rules the run produced.

CLOCK_TIME_PATTERN = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)

NUMBER_WORD = r"(?:\d+(?:[.,]\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)"

DISTANCE_PATTERN = re.compile(
    rf"\b{NUMBER_WORD}\s?(?:-|–|to)?\s?\d*(?:[.,]\d+)?\s?"
    r"(?:miles?|mi|km|kilometers?|kilometres?|metres?|meters?|blocks?|feet|ft)\b",
    re.IGNORECASE,
)

DURATION_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?(?:-|–|to)?\s?\d*\s?"
    r"(?:minutes?|mins?|hours?|hrs?|days?|nights?|weeks?|months?)\b",
    re.IGNORECASE,
)

PERCENTAGE_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?%|\b\d+\s?percent\b", re.IGNORECASE
)

# Counts of things the property has or allows. Restricted to nouns that
# describe a capability, so "two-night bookings" -- a policy, not a capacity --
# does not trip it.
CAPACITY_PATTERN = re.compile(
    rf"\b{NUMBER_WORD}\s+(?:\w+\s+)?"
    r"(?:cars?|vehicles?|spaces?|spots?|bedrooms?|beds?|bathrooms?|guests?|"
    r"people|persons?|units?|apartments?|floors?|keys?|sets?)\b",
    re.IGNORECASE,
)

NUMERIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clock_time", CLOCK_TIME_PATTERN),
    ("distance", DISTANCE_PATTERN),
    ("duration", DURATION_PATTERN),
    ("percentage", PERCENTAGE_PATTERN),
    ("capacity", CAPACITY_PATTERN),
)

SAFE = "SAFE"

REVIEW_NUMERIC_FACT = "REVIEW_NUMERIC_FACT"

REJECT_SENSITIVE = "REJECT_SENSITIVE"


def numeric_signals(text: str) -> tuple[str, ...]:
    """Which perishable operational numbers a candidate contains."""
    return tuple(
        name for name, pattern in NUMERIC_PATTERNS if pattern.search(text or "")
    )


@dataclass(frozen=True)
class SafetyVerdict:
    """Whether a candidate may be stored, and how much trust it has earned.

    Three outcomes, not two. `REVIEW_NUMERIC_FACT` is the one the first run
    proved necessary: a candidate can be worth keeping and still contain a
    number nobody has confirmed lately.
    """

    accepted: bool
    status: str = SAFE
    reasons: tuple[str, ...] = ()
    numeric_signals: tuple[str, ...] = ()
    forced_property_scope: bool = False

    @property
    def needs_numeric_review(self) -> bool:
        return self.status == REVIEW_NUMERIC_FACT

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "reasons": list(self.reasons),
            "numeric_signals": list(self.numeric_signals),
            "forced_property_scope": self.forced_property_scope,
        }


def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()

    return any(marker in lowered for marker in markers)


def check_candidate(
    title: str,
    content: str,
    property_slug: str | None,
    evidence_count: int,
    evidence_property_count: int,
) -> SafetyVerdict:
    """Decide whether a proposed rule may be stored for review.

    Scope is corrected rather than rejected where it can be: a rule proposed as
    global on evidence from one property is a *good rule with a wrong scope*, so
    it is narrowed to that property and kept. Everything else is rejected --
    there is no safe way to edit a price out of a rule automatically and still
    trust what remains.
    """
    reasons: list[str] = []

    body = f"{title}\n{content}"
    words = content.split()

    if len(words) < MIN_CONTENT_WORDS:
        reasons.append("content_too_short")

    if len(words) > MAX_CONTENT_WORDS:
        reasons.append("content_too_long")

    if evidence_count < MIN_EVIDENCE_FOR_PROPERTY_SCOPE:
        reasons.append("insufficient_evidence")

    if PRICE_PATTERN.search(body):
        reasons.append("contains_price")

    if CODE_PATTERN.search(body) or contains_any(body, CODE_WORDS):
        reasons.append("contains_access_code")

    if DATE_PATTERN.search(body):
        reasons.append("contains_specific_date")

    if contains_any(body, BOOKING_SPECIFIC_MARKERS):
        reasons.append("booking_specific")

    if contains_any(body, OVERPROMISE_MARKERS):
        reasons.append("overpromises")

    if contains_any(body, IDENTITY_MARKERS):
        reasons.append("contains_identity_or_link")

    if reasons:
        return SafetyVerdict(
            accepted=False,
            status=REJECT_SENSITIVE,
            reasons=tuple(sorted(set(reasons))),
        )

    # Perishable but useful. Kept, flagged, and put in front of the owner with
    # the number visible rather than buried.
    signals = numeric_signals(body)

    status = REVIEW_NUMERIC_FACT if signals else SAFE

    # Scope correction, not rejection.
    narrowed = (
        property_slug is None
        and evidence_property_count < MIN_PROPERTIES_FOR_GLOBAL_SCOPE
    )

    return SafetyVerdict(
        accepted=True,
        status=status,
        reasons=tuple(f"numeric:{signal}" for signal in signals),
        numeric_signals=signals,
        forced_property_scope=narrowed,
    )
