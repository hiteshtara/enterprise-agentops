"""Approved rules that disagree with each other.

Two rules can both be approved, both be about parking at the same property, and
say opposite things -- because they were approved weeks apart by someone reading
one candidate at a time. The drafting layer would then hand the model both and
let it pick, silently.

So conflicts are detected and *surfaced*, never resolved here. The console shows
them; a person decides which rule survives, usually by superseding or rejecting
one. Nothing in this module changes a status.

A property-scoped rule sitting alongside a global one is **not** a conflict.
That is the intended arrangement -- the specific rule is simply the more precise
answer for that property, which `KnowledgeStore.approved_for` already orders
first.
"""

from dataclasses import dataclass
from typing import Any

from app.knowledge_consolidation import (
    PERMISSIVE_MARKERS,
    RESTRICTIVE_MARKERS,
    contains_any,
)

OPPOSING_STANCE = "opposing_stance"

DUPLICATE_SCOPE_TOPIC = "duplicate_scope_topic"

REASON_TEXT = {
    OPPOSING_STANCE: (
        "These approved rules cover the same topic and scope but take opposite "
        "positions -- one permits what the other restricts."
    ),
    DUPLICATE_SCOPE_TOPIC: (
        "More than one approved rule covers this topic at this scope. Drafting "
        "will see all of them; consider superseding or rejecting the ones that "
        "are no longer current."
    ),
}


@dataclass(frozen=True)
class KnowledgeConflict:
    """Two or more approved rules that a person needs to look at."""

    scope: str
    topic: str
    reason: str
    knowledge_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "topic": self.topic,
            "reason": self.reason,
            "message": REASON_TEXT.get(self.reason, self.reason),
            "knowledge_refs": list(self.knowledge_refs),
        }


def find_conflicts(items: Any) -> list[KnowledgeConflict]:
    """Approved rules that overlap at the same scope and topic.

    Only APPROVED rows are considered: a proposal that disagrees with live
    policy is not a conflict, it is a candidate to reject.
    """
    approved = [
        item for item in (items or ()) if getattr(item, "status", None) == "APPROVED"
    ]

    grouped: dict[tuple[str, str], list[Any]] = {}

    for item in approved:
        grouped.setdefault((item.scope, item.topic), []).append(item)

    conflicts: list[KnowledgeConflict] = []

    for (scope, topic), group in sorted(grouped.items()):
        if len(group) < 2:
            continue

        refs = tuple(sorted(item.knowledge_ref for item in group))

        body = " ".join(f"{item.title} {item.content}" for item in group)

        # An outright disagreement is worth more alarm than mere overlap.
        reason = (
            OPPOSING_STANCE
            if contains_any(body, PERMISSIVE_MARKERS)
            and contains_any(body, RESTRICTIVE_MARKERS)
            else DUPLICATE_SCOPE_TOPIC
        )

        conflicts.append(
            KnowledgeConflict(
                scope=scope,
                topic=topic,
                reason=reason,
                knowledge_refs=refs,
            )
        )

    return conflicts
