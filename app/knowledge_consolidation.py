"""Merging near-duplicate candidates, and deciding what may become global.

Two problems the first live run exposed, both of which are about looking at the
whole set of candidates rather than one group at a time:

  * **Duplicates.** The same property produced two early-check-in rules saying
    nearly the same thing in different words, and a "one-hour cushion" appeared
    once under check-in and once under checkout. Two APPROVED rules for one
    scope and topic is a reviewer's problem and then a drafting problem.
  * **Nothing was ever global.** The model chose "property" every time, so the
    one genuinely portfolio-wide rule -- early check-in depends on the previous
    checkout -- was proposed three times, once per property.

Both are handled deterministically here, using the lexical machinery the
retrieval layer already has. No embeddings.
"""

from dataclasses import dataclass, replace
from typing import Any

from app.knowledge_safety import REVIEW_NUMERIC_FACT
from app.reply_retrieval import cosine, tokenise

# Two rules about the same topic and scope, worded differently, still say the
# same thing well above this. Chosen deliberately high: merging two rules that
# are *not* the same silently drops one, which is worse than showing the owner
# a near-duplicate they can reject in a second.
DUPLICATE_SIMILARITY = 0.55

# Slightly lower across properties: the same practice described for two
# different homes shares less incidental vocabulary.
GLOBAL_SIMILARITY = 0.45

# Six properties produced candidates in the live corpus. Three is half the
# portfolio -- enough that a practice is the business's rather than one
# building's, and strictly more than the two that could be coincidence or one
# owner habit copied once. Deliberately not two.
MIN_PROPERTIES_FOR_GLOBAL = 3

# A rule that names something only one building has cannot describe them all,
# however often it is repeated.
PROPERTY_SPECIFIC_MARKERS: tuple[str, ...] = (
    "driveway",
    "the two units",
    "both units",
    "the lower unit",
    "back area",
    "this property",
    "this home",
    "the building",
    "around the corner",
    "one block",
    "next door",
    "basement",
    "the garage",
    "street parking",
)

# Opposing stances on the same topic. If a cluster contains both, the
# properties do not actually agree and nothing may be promoted.
PERMISSIVE_MARKERS: tuple[str, ...] = (
    "is guaranteed",
    "always available",
    "can always",
    "we always",
    "any time",
    "no need to ask",
    "automatically",
)

RESTRICTIVE_MARKERS: tuple[str, ...] = (
    "not guaranteed",
    "cannot",
    "can not",
    "do not",
    "does not",
    "is not",
    "never",
    "not automatic",
    "depends on",
    "subject to",
)


@dataclass(frozen=True)
class Candidate:
    """One proposed rule, before it reaches the store."""

    property_slug: str | None
    topic: str
    title: str
    content: str
    audience: str
    reason: str | None
    safety_status: str
    safety_reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    evidence_properties: frozenset[str]
    first_observed_at: str | None
    last_observed_at: str | None

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_refs)

    @property
    def distinct_property_count(self) -> int:
        return len(self.evidence_properties)

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_slug": self.property_slug,
            "scope": self.property_slug or "global",
            "topic": self.topic,
            "title": self.title,
            "content": self.content,
            "audience": self.audience,
            "safety_status": self.safety_status,
            "safety_reasons": list(self.safety_reasons),
            "evidence_count": self.evidence_count,
            "distinct_property_count": self.distinct_property_count,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
        }


def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()

    return any(marker in lowered for marker in markers)


def similarity(left: Candidate, right: Candidate) -> float:
    """How close two rules are, by content vocabulary."""
    left_tokens = tokenise(f"{left.title} {left.content}")
    right_tokens = tokenise(f"{right.title} {right.content}")

    if not left_tokens or not right_tokens:
        return 0.0

    left_vector = {token: 1.0 for token in left_tokens}
    right_vector = {token: 1.0 for token in right_tokens}

    return cosine(left_vector, right_vector)


def merge(primary: Candidate, other: Candidate) -> Candidate:
    """Fold one candidate into another, combining their evidence.

    The wording kept is the one with more evidence behind it; the evidence is
    the union, because both were really observed.
    """
    observed = sorted(
        stamp
        for stamp in (
            primary.first_observed_at,
            primary.last_observed_at,
            other.first_observed_at,
            other.last_observed_at,
        )
        if stamp
    )

    # A merged rule inherits the more cautious safety status of the two.
    status = (
        REVIEW_NUMERIC_FACT
        if REVIEW_NUMERIC_FACT in (primary.safety_status, other.safety_status)
        else primary.safety_status
    )

    return replace(
        primary,
        safety_status=status,
        safety_reasons=tuple(
            sorted(set(primary.safety_reasons + other.safety_reasons))
        ),
        evidence_refs=tuple(sorted(set(primary.evidence_refs + other.evidence_refs))),
        evidence_properties=primary.evidence_properties | other.evidence_properties,
        first_observed_at=observed[0] if observed else None,
        last_observed_at=observed[-1] if observed else None,
    )


def consolidate(candidates: list[Candidate]) -> list[Candidate]:
    """Merge near-duplicates within each (scope, topic, audience).

    Audience is part of the key on purpose: a guest-facing rule and an internal
    procedure can share vocabulary without being the same thing.
    """
    buckets: dict[tuple[str | None, str, str], list[Candidate]] = {}

    for candidate in candidates:
        key = (candidate.property_slug, candidate.topic, candidate.audience)
        buckets.setdefault(key, []).append(candidate)

    merged: list[Candidate] = []

    for bucket in buckets.values():
        # Most evidence first, so the best-supported wording survives a merge.
        ordered = sorted(bucket, key=lambda item: item.evidence_count, reverse=True)

        kept: list[Candidate] = []

        for candidate in ordered:
            for index, existing in enumerate(kept):
                if similarity(existing, candidate) >= DUPLICATE_SIMILARITY:
                    kept[index] = merge(existing, candidate)
                    break

            else:
                kept.append(candidate)

        merged.extend(kept)

    return merged


def may_be_global(cluster: list[Candidate]) -> tuple[bool, str]:
    """Whether a cluster of same-topic rules describes the whole portfolio.

    Four conditions, all required. Any one failing keeps the rules
    property-specific, which is the safe direction: a rule that should have been
    global costs a little repetition, while a local arrangement promoted to
    global becomes a claim about six buildings on the evidence of one.
    """
    properties = set()

    for candidate in cluster:
        properties |= candidate.evidence_properties

        if candidate.property_slug:
            properties.add(candidate.property_slug)

    if len(properties) < MIN_PROPERTIES_FOR_GLOBAL:
        return False, "too_few_properties"

    for candidate in cluster:
        if contains_any(
            f"{candidate.title} {candidate.content}", PROPERTY_SPECIFIC_MARKERS
        ):
            return False, "property_specific_content"

        # A number in a rule is almost always about one place. A distance or a
        # capacity cannot describe six different buildings.
        if candidate.safety_status == REVIEW_NUMERIC_FACT:
            return False, "numeric_fact"

    body = " ".join(f"{item.title} {item.content}" for item in cluster)

    if contains_any(body, PERMISSIVE_MARKERS) and contains_any(
        body, RESTRICTIVE_MARKERS
    ):
        return False, "contradictory_evidence"

    return True, "agreed_across_properties"


def promote_global(candidates: list[Candidate]) -> tuple[list[Candidate], int]:
    """Replace agreeing per-property rules with one global rule.

    Returns the new candidate list and how many global rules were created. The
    per-property rules that agreed are removed: keeping both would put the same
    guidance in front of the model twice.
    """
    by_topic: dict[tuple[str, str], list[Candidate]] = {}

    for candidate in candidates:
        if candidate.property_slug is None:
            continue

        by_topic.setdefault((candidate.topic, candidate.audience), []).append(candidate)

    promoted: list[Candidate] = []
    absorbed: set[int] = set()

    for (topic, audience), group in by_topic.items():
        if len(group) < MIN_PROPERTIES_FOR_GLOBAL:
            continue

        # Cluster the group by mutual similarity, seeded by the best-supported.
        ordered = sorted(group, key=lambda item: item.evidence_count, reverse=True)

        seed = ordered[0]

        cluster = [
            candidate
            for candidate in ordered
            if candidate is seed or similarity(seed, candidate) >= GLOBAL_SIMILARITY
        ]

        if len({item.property_slug for item in cluster}) < MIN_PROPERTIES_FOR_GLOBAL:
            continue

        allowed, _ = may_be_global(cluster)

        if not allowed:
            continue

        combined = cluster[0]

        for candidate in cluster[1:]:
            combined = merge(combined, candidate)

        promoted.append(
            replace(
                combined,
                property_slug=None,
                topic=topic,
                audience=audience,
                evidence_properties=frozenset(
                    item.property_slug for item in cluster if item.property_slug
                )
                | combined.evidence_properties,
            )
        )

        absorbed |= {id(item) for item in cluster}

    remaining = [item for item in candidates if id(item) not in absorbed]

    return remaining + promoted, len(promoted)
