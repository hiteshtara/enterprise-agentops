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

import re
from dataclasses import dataclass
from typing import Any

from app.cancellation import POLICY_GUIDANCE as CANCELLATION_GUIDANCE
from app.cancellation import outcome_for as cancellation_outcome
from app.early_check_in import FAR_FUTURE_GUIDANCE, is_early_check_in_request
from app.early_check_in import POLICY_GUIDANCE as EARLY_CHECK_IN_GUIDANCE
from app.early_check_in import outcome_for as early_check_in_outcome
from app.historical_replies import topics_for
from app.late_checkout import (
    AUTOMATIC_CEILING,
    ESCALATION_REASON,
    STANDARD_CHECKOUT,
    is_late_checkout_request,
    requires_owner_approval,
)
from app.late_checkout import POLICY_GUIDANCE as LATE_CHECKOUT_GUIDANCE
from app.stay_extension import ESCALATION_REASON as STAY_EXTENSION_ESCALATION
from app.stay_extension import POLICY_GUIDANCE as STAY_EXTENSION_GUIDANCE
from app.stay_extension import is_extension_request

# Sender type of an outbound message, mirroring the connector's vocabulary.
# Duplicated rather than imported so this module stays free of the connector.
SENDER_OWNER = "Owner"


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


# The safe default for a *business-sensitive* question this file does not cover:
# acknowledge it and hand it to a person -- see UNDOCUMENTED_TOPICS below. It is
# no longer the default for every unruled question; see the business-sensitivity
# section further down for what decides which is which.
ACKNOWLEDGEMENT = "Thank you for your question. I'll check and get back to you shortly."


REPLY_RULES: tuple[ReplyRule, ...] = (
    ReplyRule(
        topic="general_acknowledgement",
        # The fallback used to be "a person will follow up" for every question
        # no rule covered, which made the owner the answering service for "any
        # good restaurants nearby?". The default is now split by what the
        # question could affect, not by whether a rule happens to mention it.
        guidance=(
            "An ordinary question is yours to answer. Restaurants, directions, "
            "transit, neighbourhoods, attractions, Boston, general knowledge -- "
            "anything that does not touch this property, the reservation, "
            "money, or a promise made to this guest -- gets a helpful answer "
            "from general knowledge, in the host's voice. Do not hand it to a "
            "person. "
            "Keep the acknowledgement for a business-sensitive question that "
            "neither this guidance nor approved knowledge can answer: there, "
            "acknowledge warmly and say a person will follow up. Never "
            "improvise an answer to fill that gap."
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
        # Owner-authored policy, not a distilled habit: the extra hour to
        # 11:00 AM is pre-approved, so hedging it would be wrong. Everything
        # past 11:00 AM stays the owner's decision. See app/late_checkout.py.
        guidance=LATE_CHECKOUT_GUIDANCE,
        example=(
            "We can extend checkout until 11:00 AM for you -- our regular "
            "checkout is 10:00 AM."
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
    # Narrowed deliberately. This bans vouching for a business and inventing
    # specifics about one -- not local questions as a category. A cautious model
    # read the old wording as "never discuss the neighbourhood", which is how an
    # ordinary restaurant question ended up on the owner's desk.
    (
        "a named local business presented as an endorsement, or invented "
        "specifics about one -- its address, opening hours, prices, ratings, or "
        "how far away it is"
    ),
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

# -- closing detection ----------------------------------------------------
#
# A guest ends an exchange in more ways than saying "thanks". An earlier version
# of this recognised only short acknowledgement phrases, so a perfectly clear
# closing -- "I don't worry, I was just curious. I'll have a look outside
# anyway" -- read as a live request and drew a reply nobody needed.
#
# So closure is not decided by matching a phrase list. It is decided by two
# independent questions:
#
#   1. Does the message contain anything ACTIONABLE -- a question, a request, a
#      problem, or a changed plan?
#   2. Does it contain positive evidence of CLOSURE?
#
# A message is a closing only when the answer is no to the first and yes to the
# second. Both halves matter. Requiring positive evidence is what stops silence
# being the default for a message we simply failed to understand, and a message
# carrying new information the host should acknowledge -- "we're arriving at
# 11pm" -- has no closing cue, so it still draws a reply.
#
# The asymmetry is deliberate throughout: an unnecessary draft costs the host
# ten seconds, a false NO_REPLY_NEEDED leaves a real guest ignored.

# Interrogatives that appear without a question mark. Guests routinely drop it.
QUESTION_MARKERS: tuple[str, ...] = (
    "what time",
    "what is",
    "what are",
    "what should",
    "how much",
    "how many",
    "how do",
    "how does",
    "how can",
    "where is",
    "where are",
    "where can",
    "where should",
    "where do",
    "when is",
    "when are",
    "when can",
    "when do",
    "when should",
    "which one",
    "who is",
    "is there",
    "are there",
    "is it possible",
    "do you",
    "does it",
    "did you",
    "can we",
    "can i",
    "could we",
    "could i",
    "would it",
    "should we",
    "should i",
    "any chance",
)

# Asking us to do something, however politely. "I was wondering if" belongs here
# rather than among the softeners: it almost always introduces a request.
REQUEST_MARKERS: tuple[str, ...] = (
    "please",
    "can you",
    "could you",
    "would you",
    "will you",
    "let me know",
    "let us know",
    "send me",
    "send us",
    "i need",
    "we need",
    "i would like",
    "we would like",
    "i'd like",
    "we'd like",
    "want you to",
    "was wondering",
    "am wondering",
    "wondering if",
    "confirm",
    "get back to me",
    "check for me",
)

# Something is wrong. These outrank every closing cue in the same message.
PROBLEM_MARKERS: tuple[str, ...] = (
    "not working",
    "isn't working",
    "is not working",
    "doesn't work",
    "does not work",
    "won't work",
    "won't open",
    "won't turn",
    "can't get in",
    "cannot get in",
    "broken",
    "issue",
    "problem with",
    "something wrong",
    "went wrong",
    "missing",
    "no hot water",
    "no water",
    "no heat",
    "leaking",
    "unhappy",
    "disappointed",
    "complaint",
    "refund",
    "cancel",
    "stuck",
    "dirty",
)

# New facts about the stay. Even phrased as a closing, these need a human to see
# them -- an extra guest or a late arrival changes what the host has to do.
PLAN_CHANGE_MARKERS: tuple[str, ...] = (
    "actually",
    "instead",
    "another guest",
    "extra guest",
    "additional guest",
    "one more person",
    "one more thing",
    "change",
    "changing",
    "changed",
    "reschedule",
    "rescheduling",
    "postpone",
    # Spelled out rather than stemmed: markers are matched on whole words, so a
    # stem like "arriv" would never match "arriving".
    "arrive",
    "arrives",
    "arriving",
    "arrival",
    "landing",
    "flight",
    "running late",
    "be late",
    "delayed",
    "earlier than",
    "later than",
)

# Positive evidence that the guest is closing the exchange rather than opening
# one. Grouped by what they do, not by topic.
CLOSING_MARKERS: tuple[str, ...] = (
    # thanks
    "thank you",
    "thanks",
    "thx",
    "ty",
    "appreciate it",
    "much appreciated",
    "appreciated",
    # agreement
    "sounds good",
    "sounds great",
    "ok",
    "okay",
    "perfect",
    "great",
    "awesome",
    "got it",
    "understood",
    "noted",
    "will do",
    "fair enough",
    "that works",
    "that will work",
    "that'll work",
    "works for us",
    "works for me",
    "that's fine",
    "thats fine",
    "that is fine",
    "all good",
    # de-escalation -- the guest withdrawing the request themselves
    "no worries",
    "no problem",
    "not a problem",
    "no need",
    "no rush",
    "no hurry",
    "don't worry",
    "dont worry",
    "curious",
    "just checking",
    "just asking",
    # farewell
    "see you",
    "looking forward",
    "safe travels",
)

# First-person future intent: the guest saying what *they* will do. This is the
# structural half of closure detection and the reason a novel sentence like
# "I'll have a look outside anyway" is recognised without listing it anywhere.
SELF_RESOLUTION_PATTERN = re.compile(
    r"\b(?:i|we)\s*(?:'|’)?\s*(?:ll|will|can|shall|am going to|are going to)\b",
)


def normalise_text(text: str) -> str:
    """Lowercase, with curly apostrophes folded so contractions match."""
    return text.lower().replace("’", "'")


def contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    """Whether any marker appears as a whole word or phrase."""
    return any(
        re.search(r"(?<![a-z])" + re.escape(marker) + r"(?![a-z])", text)
        for marker in markers
    )


def actionable_signals(text: str) -> tuple[str, ...]:
    """Why a message still needs a reply, named rather than implied.

    Returned to the model as well as used for the decision, so a draft can see
    *what* was detected instead of being handed a bare verdict.
    """
    if not isinstance(text, str) or not text.strip():
        return ()

    normalised = normalise_text(text)

    signals: list[str] = []

    if "?" in text:
        signals.append("question_mark")

    if contains_marker(normalised, QUESTION_MARKERS):
        signals.append("question")

    if contains_marker(normalised, REQUEST_MARKERS):
        signals.append("request")

    if contains_marker(normalised, PROBLEM_MARKERS):
        signals.append("problem")

    if contains_marker(normalised, PLAN_CHANGE_MARKERS):
        signals.append("plan_change")

    return tuple(signals)


def closing_signals(text: str) -> tuple[str, ...]:
    """Positive evidence that the guest is wrapping up."""
    if not isinstance(text, str) or not text.strip():
        return ()

    normalised = normalise_text(text)

    signals: list[str] = []

    if contains_marker(normalised, CLOSING_MARKERS):
        signals.append("closing_phrase")

    if SELF_RESOLUTION_PATTERN.search(normalised):
        signals.append("self_resolution")

    return tuple(signals)


# -- business sensitivity -------------------------------------------------
#
# The routing question, and the reason this section exists: *may the model
# answer this itself?*
#
# The old answer was "only if a rule below covers it", which made the owner the
# answering service for "any good restaurants nearby?". The new answer is the
# other way round -- an ordinary question gets answered, and the owner is asked
# only when the answer could affect the property, the reservation, money, or a
# promise to the guest.
#
# Deciding that is not left to the model. A model asked to judge its own
# authority will drift, and the direction it drifts in is the expensive one. So
# the verdict is computed here, in Python, from the same whole-word marker
# matching every other signal in this file uses, and handed to the model as a
# fact. The model is told *what* was detected, not merely that something was --
# a bare verdict is unarguable and therefore unusable.
#
# The marker sets are deliberately generous. A false positive costs a guest one
# "I'll check" on a question we could have answered; a false negative lets a
# draft speak for the business about a refund. Grouped by what a match means,
# because a category name is what the drafting model actually reads.

BUSINESS_MARKER_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "availability",
        (
            "available",
            "availability",
            "vacancy",
            "vacancies",
            "fully booked",
            "still free",
            "free that week",
            "any nights",
        ),
    ),
    (
        # Anything that changes the shape of the reservation itself.
        "reservation_change",
        (
            "reservation",
            "booking",
            "rebook",
            "reschedule",
            "change our dates",
            "change the dates",
            "change my dates",
            "move our dates",
            "move the dates",
            "extend our stay",
            "extend the stay",
            "extend my stay",
            "another night",
            "extra night",
            "extra nights",
            "more nights",
            "additional night",
            "additional nights",
            "one more night",
            "shorten our stay",
            "amend the booking",
            "modify the booking",
            "add a night",
        ),
    ),
    (
        "early_check_in",
        (
            "early check in",
            "early check-in",
            "early checkin",
            "check in early",
            "check-in early",
            "checkin early",
            "check in earlier",
            "earlier check in",
            "earlier check-in",
            "early arrival",
            "arrive early",
            "arrive earlier",
            "arriving early",
            "get in early",
            "get in earlier",
            "come early",
            # Deliberately narrow: "check in" on its own also appears in
            # "I'll check in with you later", which is not a policy question.
            "check-in",
            "checkin",
            "checking in",
            "check in at",
            "check in time",
        ),
    ),
    (
        "late_checkout",
        (
            "checkout",
            "check-out",
            "checking out",
            "check out at",
            "check out by",
            "check out time",
            "check out later",
            "check out early",
            "late check out",
            "later check out",
            "leave later",
            "stay later",
            "stay longer",
            "later departure",
            "vacate",
        ),
    ),
    (
        "parking",
        (
            # Never the bare word "park" -- Boston has several, and asking about
            # one is exactly the general question this change permits.
            "parking",
            "park the car",
            "park our car",
            "park my car",
            "where to park",
            "can i park",
            "can we park",
            "garage",
            "driveway",
        ),
    ),
    (
        # Whether a specific item is in a specific unit is recorded nowhere, and
        # a guess here becomes a promise about a real stay -- the wine-opener
        # regression in one line.
        "amenity_present",
        (
            "amenity",
            "amenities",
            "appliance",
            "appliances",
            "does the apartment have",
            "does the place have",
            "does the unit have",
            "does the flat have",
            "does the property have",
            "in the apartment",
            "in the unit",
            "blender",
            "coffee maker",
            "coffee machine",
            "kettle",
            "toaster",
            "microwave",
            "oven",
            "stove",
            "hob",
            "dishwasher",
            "washing machine",
            "washer",
            "dryer",
            "hair dryer",
            "hairdryer",
            "iron",
            "ironing board",
            "crib",
            "cot",
            "high chair",
            "tv",
            "television",
            "towels",
            "linen",
            "linens",
            "sheets",
            "pillows",
            "duvet",
            "bottle opener",
            "wine opener",
            "corkscrew",
            "cutlery",
            "utensils",
            "pots",
            "pans",
            "balcony",
            "bathtub",
            "shower",
            "fridge",
            "freezer",
        ),
    ),
    (
        "refunds",
        (
            "refund",
            "refunds",
            "refunded",
            "money back",
            "reimburse",
            "reimbursed",
            "reimbursement",
        ),
    ),
    (
        "cancellations",
        (
            "cancel",
            "cancels",
            "cancelling",
            "canceling",
            "cancelled",
            "canceled",
            "cancellation",
            "cancelation",
        ),
    ),
    (
        "discounts",
        (
            "discount",
            "discounts",
            "cheaper",
            "reduce the price",
            "better rate",
            "special rate",
            "promo",
            "promo code",
            "coupon",
            "voucher",
        ),
    ),
    (
        "pricing",
        (
            "price",
            "prices",
            "pricing",
            "cost",
            "costs",
            # Bare "how much" also matches measurement questions -- "how much
            # time does it take to get downtown", "how much walking is it to
            # the river" -- that ask about distance or duration, not money.
            # An over-broad marker escalates ordinary travel questions to the
            # owner, which is exactly what this routing exists to prevent, so
            # only money-shaped phrasings of "how much" are kept.
            "how much is",
            "how much does it cost",
            "how much do you charge",
            "how much for",
            "how much will",
            "how much would",
            "rate",
            "rates",
            "fee",
            "fees",
            "charge",
            "charges",
            "deposit",
            "deposits",
            "tax",
            "taxes",
            "quote",
            "surcharge",
        ),
    ),
    (
        "payments",
        (
            "pay",
            "paid",
            "payment",
            "payments",
            "invoice",
            "receipt",
            "credit card",
            "card details",
            "bank transfer",
            "billing",
            "billed",
        ),
    ),
    (
        # Handing out an access credential is not a message, it is a key.
        "access_credentials",
        (
            "access code",
            "access instructions",
            "check-in code",
            "check in code",
            "check-in instructions",
            "check in instructions",
            "door code",
            "entry code",
            "gate code",
            "building code",
            "key code",
            "keypad",
            "lockbox",
            "lock box",
            "key box",
            "password",
            "wifi password",
            "wi-fi password",
        ),
    ),
    (
        "additional_guests",
        (
            "another guest",
            "extra guest",
            "extra guests",
            "additional guest",
            "additional guests",
            "one more person",
            "extra person",
            "extra people",
            "more people",
            "bring a friend",
            "bring friends",
            "bring my friend",
            "visitor",
            "visitors",
            "sleeps",
        ),
    ),
    (
        "pets",
        (
            "pet",
            "pets",
            "dog",
            "dogs",
            "cat",
            "cats",
            "puppy",
            "kitten",
            "animal",
            "animals",
            "service animal",
            "emotional support",
        ),
    ),
    (
        "damage_maintenance_safety",
        (
            "broken",
            "damage",
            "damaged",
            "leak",
            "leaks",
            "leaking",
            "not working",
            "isn't working",
            "is not working",
            "doesn't work",
            "does not work",
            "won't work",
            "won't open",
            "won't close",
            "blocked",
            "clogged",
            "repair",
            "fix",
            "maintenance",
            "plumber",
            "electrician",
            "flood",
            "flooding",
            "mould",
            "mold",
            "pest",
            "bugs",
            "cockroach",
            "unsafe",
            "hazard",
            "fire alarm",
            "smoke alarm",
            "carbon monoxide",
        ),
    ),
    # -- the categories the owner added -----------------------------------
    (
        "cleaning",
        (
            "clean",
            "cleaned",
            "cleaner",
            "cleaning",
            "housekeeping",
            "house keeping",
            "maid",
            "dirty",
            "unclean",
            "hoover",
            "vacuum",
            "rubbish",
            "trash",
            "bin",
            "bins",
            "garbage",
            "fresh towels",
        ),
    ),
    (
        "noise",
        (
            "noise",
            "noisy",
            "loud",
            "loudly",
            # The trailing word boundary is what keeps "neighbourhood" -- a
            # perfectly ordinary local question -- out of this category.
            "neighbour",
            "neighbours",
            "neighbor",
            "neighbors",
            "banging",
            "shouting",
            "upstairs",
            "downstairs",
            "next door",
        ),
    ),
    (
        "lost_and_found",
        (
            "lost",
            "lost property",
            "lost and found",
            "left behind",
            "left my",
            "left our",
            "left it",
            "left them",
            "forgot my",
            "forgot our",
            "forgotten",
            "misplaced",
            "missing",
        ),
    ),
    (
        "smoking",
        (
            "smoke",
            "smoking",
            "smoker",
            "cigarette",
            "cigarettes",
            "cigar",
            "vape",
            "vaping",
            "shisha",
        ),
    ),
    (
        "parties_events",
        (
            # Not the bare word "event": "any events this weekend?" is a
            # perfectly ordinary question about the city.
            "party",
            "parties",
            "gathering",
            "celebration",
            "birthday party",
            "have people over",
            "have friends over",
            "bachelor party",
            "bachelorette",
        ),
    ),
    (
        "luggage_storage",
        (
            "luggage",
            "luggage storage",
            "left luggage",
            "bag storage",
            "store our bags",
            "store my bags",
            "leave our bags",
            "leave my bags",
            "leave our luggage",
            "drop our bags",
            "drop off our bags",
            "drop bags",
            "suitcase",
            "suitcases",
        ),
    ),
    (
        "deliveries",
        (
            "delivery",
            "deliveries",
            "deliver",
            "delivered",
            "package",
            "packages",
            "parcel",
            "parcels",
            "courier",
            "fedex",
            "mail",
        ),
    ),
    (
        "accessibility",
        (
            "accessible",
            "accessibility",
            "wheelchair",
            "step-free",
            "step free",
            "stairs",
            "elevator",
            "lift",
            "mobility",
            "disabled",
            "disability",
            "handrail",
            "ramp",
            "ground floor",
        ),
    ),
    (
        "compensation",
        (
            "compensation",
            "compensate",
            "compensated",
            "credit",
            "credits",
            "goodwill",
            "partial refund",
            "make it right",
        ),
    ),
    (
        "internet",
        (
            "wifi",
            "wi-fi",
            "internet",
            "broadband",
            "router",
            "modem",
            "hotspot",
            "network",
            "get online",
        ),
    ),
    (
        "utilities",
        (
            "heat",
            "heating",
            "radiator",
            "boiler",
            "thermostat",
            "hot water",
            "cold water",
            "no water",
            "water pressure",
            "air conditioning",
            "air con",
            "aircon",
            "a/c",
            "electricity",
            "power",
            "fuse",
            "breaker",
        ),
    ),
    (
        "security",
        (
            "security",
            "safety",
            "safe",
            "unlocked",
            "break-in",
            "broke in",
            "intruder",
            "stolen",
            "theft",
            "burglary",
            "cctv",
            "camera",
            "cameras",
            "surveillance",
            "suspicious",
        ),
    ),
    (
        "keys_lockouts",
        (
            "key",
            "keys",
            "keycard",
            "key card",
            "key fob",
            "spare key",
            "locked out",
            "lock out",
            "lockout",
            "locked myself out",
            "can't get in",
            "cannot get in",
            "front door",
            "door lock",
            "lock",
        ),
    ),
)

# The flat set `contains_marker` consumes. Built from the groups rather than
# maintained beside them, so the two cannot disagree about what is covered.
BUSINESS_MARKERS: tuple[str, ...] = tuple(
    marker for _category, markers in BUSINESS_MARKER_GROUPS for marker in markers
)


def business_categories(text: Any) -> tuple[str, ...]:
    """Which business-sensitive categories a message touches, named.

    Named rather than counted for the same reason `actionable_signals` names
    its signals: a draft that can see *what* was detected can answer the rest of
    the message, while a bare verdict can only be obeyed or ignored.
    """
    if not isinstance(text, str) or not text.strip():
        return ()

    normalised = normalise_text(text)

    return tuple(
        category
        for category, markers in BUSINESS_MARKER_GROUPS
        if contains_marker(normalised, markers)
    )


def is_business_sensitive(text: Any) -> bool:
    """Whether one message touches the property, the booking, money or a promise.

    Any single marker is enough. A message is not general because most of it is
    -- "any good restaurants nearby, and can we check out at noon?" is a
    checkout question that happens to also ask about dinner.
    """
    return bool(business_categories(text))


# What the model is told when the open message is ordinary. Attached
# conditionally, like every other topic guidance in this file: a rule says how
# to answer a topic *if it is raised*, and nothing here may cause a draft to
# volunteer local information the guest did not ask for.
GENERAL_QUESTION_GUIDANCE = (
    "Ordinary questions are yours to answer. When the guest's open message does "
    "not touch this property, the reservation, money, or a promise made to "
    "them, answer it yourself from general knowledge -- restaurants and cafes, "
    "directions, transit, neighbourhoods, attractions, Boston, or anything "
    "else a well-informed local host would know. Do not hand it to a person and "
    "do not say someone will follow up: that is for questions this guidance "
    "genuinely cannot answer. Answer only what was asked -- a question the "
    "guest did not ask is not an opening to recommend things."
)

GENERAL_KNOWLEDGE_PERMISSION = (
    "Nothing in the guest's open message is business-sensitive. Answer it "
    "yourself, in the host's voice, from general knowledge. never_invent still "
    "binds in full: general knowledge is not permission to state an exact "
    "opening hour, rating, travel time, walking distance, price or transit "
    "departure."
)

BUSINESS_SENSITIVE_ROUTING = (
    "The guest's open message touches business-sensitive ground -- "
    "business_sensitivity.categories names which. Handle that part through the "
    "policy, approved knowledge and escalation rules in this guidance, and "
    "do not answer it from general knowledge. If the same message also asks "
    "something ordinary -- a restaurant, a direction, a neighbourhood -- answer "
    "that part helpfully in the same reply. A message is not general because "
    "part of it is, and the ordinary half is not off-limits because the other "
    "half is. never_invent binds throughout."
)

# The fabrication guard, appended to never_invent rather than replacing it.
#
# It is deliberately independent of the routing verdict above. Routing decides
# *who answers*; this decides *what may be asserted*, and it binds even when
# routing says the question is general. That independence is the whole point:
# the marker sets will miss something eventually, and when they do, this is
# what stops a missed marker from becoming an invented four-minute walk.
EXACT_FACT_GUARD = (
    "The same rule covers exact facts about the world, not only about the "
    "property. An exact opening hour, a rating, a travel time, a walking "
    "distance, a price, or a real-time transit schedule needs a live "
    "authoritative source -- never your own recollection, however confident. "
    "Without one, stay general: 'there are several restaurants and cafes in "
    "the neighbourhood' and 'you can take public transit toward downtown' are "
    "fine. 'An Italian restaurant two minutes from your front door' and 'the "
    "nearest station is a 4-minute walk' are not."
)


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
        "If the guest's latest message closes the exchange and nothing is open, "
        "either send one short friendly closing line or reply with "
        f"{NO_REPLY_NEEDED}. Do not manufacture an operational explanation just "
        "because earlier messages contained questions."
    ),
    (
        "A closing is not only 'thanks'. A guest who accepts an answer, "
        "withdraws their own request, or says what they will do themselves -- "
        "'no worries, I was just curious', 'that's fine, we'll sort it out' -- "
        "is ending the conversation. Do not answer a question they have stopped "
        "asking, and do not restate a limitation they have already accepted."
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


def is_closing_message(text: str) -> bool:
    """Whether a guest message closes the exchange rather than opening one.

    True only when nothing actionable is present *and* there is positive
    evidence of closure. Length is not a criterion: a two-sentence message that
    accepts an answer and says what the guest will do themselves is as final as
    "Thanks", and the earlier word-count rule is exactly what missed it.

    Both halves are load-bearing. Dropping the actionable check would silence
    real questions; dropping the closure requirement would make silence the
    default for any message the rules failed to parse.
    """
    if actionable_signals(text):
        return False

    return bool(closing_signals(text))


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

    latest_is_closing = bool(
        latest_guest and is_closing_message(latest_guest["message"])
    )

    # Every reason any still-open message needs answering, so the model sees
    # what was detected rather than a bare verdict it has to take on trust.
    open_signals = sorted(
        {signal for row in unanswered for signal in actionable_signals(row["message"])}
    )

    # A request to check out past the ceiling is the owner's call. Computed
    # from the still-open messages only, so a request we already answered does
    # not escalate the thread forever.
    late_checkout_open = any(
        is_late_checkout_request(row["message"]) for row in unanswered
    )

    beyond_policy = requires_owner_approval(unanswered)

    early_check_in_open = any(
        is_early_check_in_request(row["message"]) for row in unanswered
    )

    # Whether anything still open could affect the property, the reservation,
    # money, or a promise to the guest. Computed from the same unanswered list
    # as every other topic verdict, so a business question the owner already
    # answered does not keep routing the thread away from the model.
    open_business_categories = sorted(
        {
            category
            for row in unanswered
            for category in business_categories(row["message"])
        }
    )

    # Asking for more nights than were booked. Same list, same reason: a
    # request the owner has already answered must not keep re-firing, and the
    # unanswered messages are the only ones that can still be open.
    stay_extension_open = any(
        is_extension_request(row["message"]) for row in unanswered
    )

    if not unanswered:
        outcome = "already_replied"

    elif all(is_closing_message(row["message"]) for row in unanswered):
        outcome = "no_reply_needed"

    else:
        outcome = "reply_needed"

    return {
        "message_count": len(rows),
        "latest_sender": rows[-1]["sender"] if rows else None,
        "awaiting_our_reply": bool(unanswered),
        "unanswered_guest_messages": [row["message"] for row in unanswered],
        "latest_guest_message": latest_guest["message"] if latest_guest else None,
        "latest_guest_message_is_closing": latest_is_closing,
        "open_signals": open_signals,
        "business_sensitive": bool(open_business_categories),
        "business_categories": open_business_categories,
        "answered_earlier_by_us": [
            row["message"]
            for index, row in enumerate(rows)
            if index < last_owner_index and row["sender"] == "Renter"
        ],
        "suggested_outcome": outcome,
        "late_checkout_requested": late_checkout_open,
        "early_check_in_requested": early_check_in_open,
        "stay_extension_requested": stay_extension_open,
        "late_checkout_beyond_policy": beyond_policy,
        "owner_approval_required": beyond_policy,
        "owner_approval_reason": ESCALATION_REASON if beyond_policy else None,
        "checkout_policy": {
            "standard_checkout": STANDARD_CHECKOUT,
            "automatic_ceiling": AUTOMATIC_CEILING,
        },
        "note": (
            "answered_earlier_by_us lists guest messages a later Owner message "
            "already responded to. Do not answer those again. open_signals "
            "names why anything still open needs a reply -- an empty list with "
            "unanswered messages present means the guest was closing the "
            "conversation, not asking for anything. business_sensitive says "
            "whether anything still open could affect the property, the "
            "reservation, money or a promise to this guest, and "
            "business_categories names which ones. suggested_outcome is advice "
            "from a simple rule -- read the conversation and override it if the "
            "wording tells you otherwise."
        ),
    }


# -- authority ------------------------------------------------------------
#
# Once historical replies became drafting context, "what wins when sources
# disagree" stopped being obvious. A real reply from March saying parking is
# free is genuinely how the owner writes, and genuinely wrong today. The order
# below is what keeps the first fact from becoming the second.

AUTHORITY_ORDER: tuple[str, ...] = (
    (
        "1. LIVE AUTHORITATIVE SYSTEM DATA -- tool results: live availability, "
        "current pricing, reservation state. Always wins."
    ),
    (
        "2. AN EXPLICIT COMMITMENT ALREADY MADE TO THIS GUEST in this "
        "conversation. If the host has already told this guest something "
        "specific, that stands -- even where general policy says otherwise. See "
        "current_conversation_exceptions."
    ),
    (
        "3. OWNER-APPROVED PRIYANKA HOMES KNOWLEDGE -- the reviewed rules in "
        "approved_knowledge, together with the standing topic rules in `rules`. "
        "Both are authoritative for anything not already promised above. Where "
        "an approved rule scoped to this property and a general topic rule "
        "disagree, the property-scoped one is more specific and wins; where "
        "they genuinely conflict about what may be promised, take the more "
        "cautious of the two and say you will confirm."
    ),
    (
        "4. THE CURRENT CONVERSATION -- everything else this guest and this host "
        "have said in this thread."
    ),
    (
        "5. HISTORICAL EXAMPLES -- how the owner has answered similar questions "
        "before. Style and precedent only, never facts."
    ),
    "6. YOUR OWN GENERAL KNOWLEDGE -- last, and never about this property.",
)

# -- commitments already made ---------------------------------------------
#
# Rank 2 exists because approved policy and a promise can disagree, and the
# promise has to win. If the owner told this guest "you can check in at noon",
# a general rule saying early check-in is never guaranteed must not cause the
# next draft to walk that back. Retracting a promise costs more trust than the
# inconsistency costs.
#
# The exception is scoped to this guest and this thread. It is a fact about one
# conversation, never a new rule -- which is the other half of the semantics:
# honour it here, and do not generalise it.

COMMITMENT_MARKERS: tuple[str, ...] = (
    "you can",
    "you may",
    "you're welcome to",
    "you are welcome to",
    "that works",
    "that's fine",
    "no problem",
    "confirmed",
    "i've arranged",
    "i have arranged",
    "it's arranged",
    "i've booked",
    "we've held",
    "i'll hold",
    "we can do",
    "go ahead",
    "sure",
    "yes",
)

CONVERSATION_EXCEPTION_LABEL = "CURRENT_CONVERSATION_EXCEPTION"

CONVERSATION_EXCEPTION_GUIDANCE = (
    "The host has already committed to something for this guest that the "
    "approved rule below does not generally allow. Honour the commitment: write "
    "as though it stands, because it does. Do not repeat the general policy at "
    "the guest, do not hedge the promise, and do not apologise for the "
    "inconsistency. Equally, do not treat this as a new rule -- it applies to "
    "this guest and this conversation only, and the approved knowledge is "
    "unchanged for everyone else."
)


def looks_like_commitment(text: str) -> bool:
    """Whether an owner message reads as a specific promise to this guest.

    Deliberately loose. A false positive shows the model a commitment marker it
    can weigh against the thread it can already see; a false negative lets a
    draft contradict a promise the host actually made, which a guest experiences
    as the business going back on its word.
    """
    if not isinstance(text, str) or not text.strip():
        return False

    lowered = normalise_text(text)

    return contains_marker(lowered, COMMITMENT_MARKERS)


def conversation_exceptions(
    messages: Any,
    approved: Any = (),
) -> list[dict[str, Any]]:
    """Approved rules this conversation has already made an exception to.

    An exception is recorded when an owner message in this thread both reads as
    a commitment and touches the same topic as an approved rule. That is a
    heuristic pairing, and it is presented as one: the model gets the rule, the
    message, and an instruction to honour what was actually said.
    """
    rows = [row for row in (messages or []) if isinstance(row, dict)]

    commitments = [
        row.get("message") or ""
        for row in rows
        if row.get("sender") == SENDER_OWNER
        and looks_like_commitment(row.get("message") or "")
    ]

    if not commitments:
        return []

    exceptions: list[dict[str, Any]] = []

    for item in approved or ():
        topic = item.get("topic") if isinstance(item, dict) else None

        if not topic:
            continue

        for commitment in commitments:
            if topic in topics_for(commitment):
                exceptions.append(
                    {
                        "marker": CONVERSATION_EXCEPTION_LABEL,
                        "topic": topic,
                        "approved_rule": item.get("content"),
                        "commitment_made_in_this_thread": commitment,
                        "instruction": CONVERSATION_EXCEPTION_GUIDANCE,
                    }
                )

                break

    return exceptions


HISTORICAL_EXAMPLE_CAVEAT = (
    "These are real past replies from this owner, retrieved because the guest's "
    "question resembles them. They show you how this host writes: short, direct, "
    "practical. Copy the VOICE. "
    "Do not copy anything else. They may be months old, about a different "
    "property, or superseded by a rule in this guidance -- a past reply saying "
    "parking is free does not make parking free today. "
    "Never carry over a name, a date, a price, an access code, a property-"
    "specific detail, or a promise from an example. Never reproduce one "
    "verbatim just because it is similar. Write a fresh reply for this guest, "
    "in this conversation, using current facts only."
)


def rows_of(messages: Any) -> list[dict[str, Any]]:
    return [row for row in (messages or []) if isinstance(row, dict)]


# -- stay extension --------------------------------------------------------
#
# Detected, never decided. AgentGuard works nothing out about the calendar for
# an extension: no availability read, no booking-overlap scan, no dates
# extracted, no verdict. An open request escalates to the owner, and the only
# thing this layer contributes is the wording -- what the prepared reply may
# never say while it waits for a person.
#
# Attached conditionally, for the same reason every other topic rule is: a rule
# says how to answer a topic *if it is open*, and a thread that once discussed
# extra nights must not keep answering about them.


def reply_guidance(
    messages: Any = (),
    approved_knowledge: Any = (),
    turnover: Any = None,
    booking_cancelled: bool | None = None,
) -> dict[str, Any]:
    """The knowledge a model needs to draft a Priyanka Homes reply.

    Returned alongside a conversation rather than exposed as its own tool: the
    model should never have to decide whether to go and look up the house rules
    before answering a guest. It gets them with the conversation, every time.

    `conversation_state` is included for the same reason. Topic rules alone
    produce a reply about every subject the thread has ever touched; the state
    block is what makes a reply about the subject still open.
    """
    knowledge = [item for item in (approved_knowledge or ()) if isinstance(item, dict)]

    exceptions = conversation_exceptions(messages, knowledge)

    state = analyse_conversation(messages)

    guidance: dict[str, Any] = {
        "authority_order": list(AUTHORITY_ORDER),
        "approved_knowledge": knowledge,
        "approved_knowledge_authority": (
            "Rank 3 of 6. These are reviewed and approved by the owner and are "
            "authoritative -- unlike historical examples. State them as fact. "
            "The one thing that outranks them is a commitment already made to "
            "this guest in this thread."
        ),
        "current_conversation_exceptions": exceptions,
        "conversation_state": state,
        "late_checkout_policy": LATE_CHECKOUT_GUIDANCE,
        "how_to_read_the_conversation": list(CONVERSATION_STATE_RULES),
        "no_reply_needed": NO_REPLY_GUIDANCE,
        "historical_examples_caveat": HISTORICAL_EXAMPLE_CAVEAT,
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
            "promise with words like 'should have' or 'I believe'. "
            f"{EXACT_FACT_GUARD}"
        ),
        "escalation": (
            "If the question touches anything in do_not_answer_from_memory and "
            "is not covered by approved_knowledge, use the acknowledgement and "
            "let a person answer. Approved knowledge is the exception: it has "
            "been reviewed, so answer from it directly. An honest 'I'll check' "
            "is always acceptable; a confident guess is not."
        ),
    }

    if state.get("open_signals") or state.get("business_sensitive"):
        # Attached only while something is actually open, exactly like every
        # other topic guidance here. A thread with nothing outstanding must not
        # carry an invitation to talk about restaurants.
        guidance["general_question_policy"] = GENERAL_QUESTION_GUIDANCE

        guidance["business_sensitivity"] = {
            "business_sensitive": state.get("business_sensitive", False),
            "categories": state.get("business_categories", []),
            "how_to_answer": (
                BUSINESS_SENSITIVE_ROUTING
                if state.get("business_sensitive")
                else GENERAL_KNOWLEDGE_PERMISSION
            ),
        }

    if state.get("early_check_in_requested"):
        guidance["early_check_in_policy"] = EARLY_CHECK_IN_GUIDANCE

        verdict, reason, needs_owner = early_check_in_outcome(turnover)

        guidance["arrival_day_turnover"] = {
            # The guest's own arrival date and one verdict. Nothing about the
            # other reservation -- no dates, no status, no identity.
            "arrival_date": (
                turnover.get("arrival_date") if isinstance(turnover, dict) else None
            ),
            "early_check_in": verdict,
            "why": reason,
            # No lookup was made at all -- the arrival is not near enough for
            # the schedule to mean anything yet.
            "how_to_answer": FAR_FUTURE_GUIDANCE if turnover is None else None,
        }

        if needs_owner:
            state = {**state, "owner_approval_required": True}
            state["owner_approval_reason"] = reason

    if state.get("stay_extension_requested"):
        # Attached only while the request is open, like every other topic rule.
        # There is no state block to go with it: nothing was looked up, and an
        # extension is the owner's decision rather than a verdict to publish.
        guidance["stay_extension_policy"] = STAY_EXTENSION_GUIDANCE

        state = {**state, "owner_approval_required": True}
        state["owner_approval_reason"] = STAY_EXTENSION_ESCALATION

    offer, cancellation_escalates, cancellation_reason = cancellation_outcome(
        booking_cancelled, rows_of(messages)
    )

    if offer or cancellation_escalates or booking_cancelled is True:
        guidance["cancellation_policy"] = CANCELLATION_GUIDANCE
        guidance["cancellation_state"] = {
            # From authoritative booking state, never from the thread's wording.
            "reservation_cancelled": booking_cancelled,
            "make_retention_offer": offer,
            "why": cancellation_reason,
        }

    if cancellation_escalates:
        state = {**state, "owner_approval_required": True}
        state["owner_approval_reason"] = cancellation_reason

    if state.get("owner_approval_required"):
        # Stated separately from conversation_state so it cannot be missed in a
        # long block. The runtime enforces this regardless of what the model
        # does with it -- see app/conversation_refresh.py.
        guidance["owner_approval_required"] = state.get("owner_approval_reason")

    return guidance
