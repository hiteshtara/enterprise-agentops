"""Propose Priyanka Homes knowledge from the historical archive.

Explicit and manual:

    uv run --env-file .env python -m app.distill_knowledge

Never on import, never after indexing, never on a schedule. This spends model
calls and produces claims about a real business; it happens when a person asks
for it.

**Everything it writes is PROPOSED.** There is no code path here that approves
anything, and there must never be one -- the whole value of this pipeline is
that a human stands between "the owner said this fifteen times" and "this is
what Priyanka Homes says".

What the model sees: sanitized owner replies only, grouped by property and
topic. Guest questions are *not* sent -- the rule lives in the answers, and the
questions are the half more likely to carry personal detail. No identifier, no
raw payload, no guest text.
"""

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.database import Database
from app.historical_replies import HistoricalReplyStore
from app.knowledge import KnowledgeSource, KnowledgeStore
from app.knowledge_consolidation import Candidate, consolidate, promote_global
from app.knowledge_safety import REVIEW_NUMERIC_FACT, check_candidate
from app.knowledge_topics import (
    GUEST_FACING,
    INTERNAL_OPERATION,
    classify_audience,
    derive_topic,
)
from app.model_provider import ModelProvider, OpenAIModelProvider

# A group needs enough replies to show a practice rather than an incident.
MIN_GROUP_SIZE = 2

# Enough to establish a pattern; more than this and the prompt is mostly
# repetition, paying tokens for no extra signal.
MAX_REPLIES_PER_GROUP = 12

MAX_REPLY_CHARS = 400

INSTRUCTIONS = """\
You are helping a short-term rental owner write down the operating rules their
own past replies already imply.

Below are real replies this owner sent to guests, grouped by one property and
one topic. Guest messages are not included.

Extract at most 2 pieces of durable operational knowledge. A good rule is one a
new member of staff could follow next month.

Rules you must follow:
- Only state what the replies actually and repeatedly show. Do not infer a rule
  from a single reply, and do not invent detail to make a rule sound complete.
- Distinguish style from substance. "The owner is warm and brief" is style, not
  knowledge. Do not propose it.
- Do not include any price, fee or amount.
- Do not include any door code, lockbox code, wifi password or access detail.
- Do not include a specific date.
- Do not include anything about one particular guest, booking or stay.
- Do not promise or guarantee anything that depends on cleaning or on the
  previous guest's checkout -- describe what it depends on instead.
- Do not treat a one-off exception as policy.
- Write the content as plain prose an owner would recognise as their own policy,
  in 1-3 sentences.

If the replies show no durable rule, return an empty list. That is a good
answer; a made-up rule is not.

Return ONLY JSON, no prose around it, in exactly this shape:

{"candidates": [{"title": "...", "content": "...", "reason": "...",
"scope": "property"}]}

"scope" is "property" unless the rule is obviously true of any property of any
owner, in which case use "global".

Property: {property_label}
Topic: {topic}

Replies:
{replies}
"""


@dataclass
class DistillationReport:
    """Counts and cost. Never carries reply text."""

    examples_considered: int = 0
    groups_total: int = 0
    groups_analysed: int = 0
    groups_skipped_small: int = 0
    model_calls: int = 0
    model_failures: int = 0
    candidates_returned: int = 0
    candidates_rejected: int = 0
    candidates_consolidated: int = 0
    candidates_stored: int = 0
    candidates_updated: int = 0
    proposed_cleared: int = 0
    topic_reclassified: int = 0
    numeric_review: int = 0
    guest_facing: int = 0
    internal_operation: int = 0
    global_candidates: int = 0
    property_candidates: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    topics: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "examples_considered": self.examples_considered,
            "groups_total": self.groups_total,
            "groups_analysed": self.groups_analysed,
            "groups_skipped_small": self.groups_skipped_small,
            "model_calls": self.model_calls,
            "model_failures": self.model_failures,
            "candidates_returned": self.candidates_returned,
            "candidates_rejected": self.candidates_rejected,
            "candidates_consolidated": self.candidates_consolidated,
            "candidates_stored": self.candidates_stored,
            "candidates_updated": self.candidates_updated,
            "proposed_cleared": self.proposed_cleared,
            "topic_reclassified": self.topic_reclassified,
            "numeric_review": self.numeric_review,
            "guest_facing": self.guest_facing,
            "internal_operation": self.internal_operation,
            "global_candidates": self.global_candidates,
            "property_candidates": self.property_candidates,
            "rejection_reasons": dict(self.rejection_reasons),
            "topics": dict(self.topics),
        }


def group_examples(
    examples: list[dict[str, Any]],
) -> dict[tuple[str | None, str], list[dict[str, Any]]]:
    """Group by (property, topic).

    An example with several topics joins each of its groups: a reply covering
    parking and check-in is evidence for both.
    """
    groups: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)

    for example in examples:
        for topic in example.get("topics") or []:
            groups[(example.get("property_slug"), topic)].append(example)

    return dict(groups)


def parse_candidates(text: str) -> list[dict[str, Any]]:
    """Read the model's JSON, tolerating fences and surrounding prose.

    A malformed answer yields no candidates rather than an exception: one bad
    group must not abort a run over a hundred of them.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    body = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.DOTALL)

    if fenced:
        body = fenced.group(1).strip()

    else:
        start = body.find("{")
        end = body.rfind("}")

        if start != -1 and end > start:
            body = body[start : end + 1]

    try:
        payload = json.loads(body)

    except (TypeError, ValueError):
        return []

    if not isinstance(payload, dict):
        return []

    candidates = payload.get("candidates")

    return (
        [row for row in candidates if isinstance(row, dict)]
        if isinstance(candidates, list)
        else []
    )


def build_prompt(
    property_slug: str | None,
    topic: str,
    replies: list[str],
) -> str:
    numbered = "\n".join(
        f"{index}. {reply[:MAX_REPLY_CHARS]}"
        for index, reply in enumerate(replies[:MAX_REPLIES_PER_GROUP], start=1)
    )

    return (
        INSTRUCTIONS.replace("{property_label}", property_slug or "several properties")
        .replace("{topic}", topic)
        .replace("{replies}", numbered)
    )


def distil(
    examples_store: HistoricalReplyStore,
    knowledge_store: KnowledgeStore,
    model: ModelProvider,
    replace_proposed: bool = True,
    progress=None,
) -> DistillationReport:
    """Analyse the archive and record candidates for review.

    Three phases, because the interesting decisions need to see every candidate
    rather than one group at a time:

      1. **Collect.** Ask the model per (property, topic) group. Re-derive the
         topic from what it actually wrote, classify the audience, and run the
         safety filter.
      2. **Consolidate.** Merge near-duplicates, then promote to global any rule
         several properties independently agree on.
      3. **Persist.** Clear the previous unreviewed queue and store the new one.

    `replace_proposed` clears PROPOSED rows only. APPROVED and REJECTED rows are
    decisions a person made; nothing here may destroy them.
    """
    report = DistillationReport()

    examples = examples_store.all_examples()

    report.examples_considered = len(examples)

    groups = group_examples(examples)

    report.groups_total = len(groups)

    collected: list[Candidate] = []

    for (property_slug, topic), members in sorted(
        groups.items(), key=lambda item: (item[0][1], item[0][0] or "")
    ):
        if len(members) < MIN_GROUP_SIZE:
            report.groups_skipped_small += 1
            continue

        report.groups_analysed += 1

        # Owner replies only. The rule lives in the answers, and the questions
        # are the half more likely to carry personal detail.
        replies = [member["owner_text"] for member in members]

        try:
            report.model_calls += 1
            answer = model.generate(build_prompt(property_slug, topic, replies))

        except Exception:  # noqa: BLE001 -- one bad group must not end the run
            report.model_failures += 1
            continue

        candidates = parse_candidates(answer)

        report.candidates_returned += len(candidates)

        refs = tuple(member["example_ref"] for member in members)
        observed = sorted(
            member["created_at"] for member in members if member.get("created_at")
        )
        properties = frozenset(
            member["property_slug"] for member in members if member.get("property_slug")
        )

        for candidate in candidates:
            title = candidate.get("title")
            content = candidate.get("content")

            if not isinstance(title, str) or not isinstance(content, str):
                report.candidates_rejected += 1
                report.rejection_reasons["malformed"] = (
                    report.rejection_reasons.get("malformed", 0) + 1
                )
                continue

            # The topic the rule is actually about, not the topic of the guest
            # question that prompted it. The group's topic is only a fallback.
            derived = derive_topic(title, content, fallback=topic)

            if derived != topic:
                report.topic_reclassified += 1

            audience = classify_audience(title, content)

            verdict = check_candidate(
                title=title,
                content=content,
                property_slug=property_slug,
                evidence_count=len(members),
                evidence_property_count=max(len(properties), 1),
            )

            if not verdict.accepted:
                report.candidates_rejected += 1

                for reason in verdict.reasons:
                    report.rejection_reasons[reason] = (
                        report.rejection_reasons.get(reason, 0) + 1
                    )

                continue

            collected.append(
                Candidate(
                    # Scope always starts property-specific. Global is earned
                    # in phase 2 from cross-property agreement, never claimed
                    # by the model.
                    property_slug=property_slug,
                    topic=derived,
                    title=title,
                    content=content,
                    audience=audience,
                    reason=candidate.get("reason")
                    if isinstance(candidate.get("reason"), str)
                    else None,
                    safety_status=verdict.status,
                    safety_reasons=verdict.reasons,
                    evidence_refs=refs,
                    evidence_properties=properties or frozenset(),
                    first_observed_at=observed[0] if observed else None,
                    last_observed_at=observed[-1] if observed else None,
                )
            )

        if progress is not None and report.groups_analysed % 10 == 0:
            progress(f"  groups analysed: {report.groups_analysed}")

    # -- phase 2: consolidate ---------------------------------------------

    before = len(collected)

    merged = consolidate(collected)

    promoted, global_count = promote_global(merged)

    report.candidates_consolidated = before - len(promoted)
    report.global_candidates = global_count
    report.property_candidates = sum(
        1 for item in promoted if item.property_slug is not None
    )

    # -- phase 3: persist --------------------------------------------------

    if replace_proposed:
        report.proposed_cleared = knowledge_store.clear_proposed()

    for item in promoted:
        if item.safety_status == REVIEW_NUMERIC_FACT:
            report.numeric_review += 1

        if item.audience == GUEST_FACING:
            report.guest_facing += 1

        elif item.audience == INTERNAL_OPERATION:
            report.internal_operation += 1

        _, created = knowledge_store.propose(
            property_slug=item.property_slug,
            topic=item.topic,
            title=item.title,
            content=item.content,
            source_type=KnowledgeSource.HISTORICAL_DISTILLATION.value,
            audience=item.audience,
            safety_status=item.safety_status,
            safety_reasons=item.safety_reasons,
            reason=item.reason,
            evidence_refs=item.evidence_refs,
            evidence_property_count=max(item.distinct_property_count, 1),
            first_observed_at=item.first_observed_at,
            last_observed_at=item.last_observed_at,
        )

        if created:
            report.candidates_stored += 1
            report.topics[item.topic] = report.topics.get(item.topic, 0) + 1

        else:
            report.candidates_updated += 1

    return report


def main() -> int:
    database = Database()

    examples = HistoricalReplyStore(database=database)

    if examples.count() == 0:
        print(
            "The historical index is empty. Run "
            "`python -m app.index_lodgify_history` first.",
            file=sys.stderr,
        )

        return 1

    print("Distilling knowledge from the historical archive…")
    print("Every candidate is stored as PROPOSED. Nothing is approved here.")
    print()

    report = distil(
        examples_store=examples,
        knowledge_store=KnowledgeStore(database=database),
        model=OpenAIModelProvider(),
        progress=print,
    )

    print()

    for label, value in report.to_dict().items():
        print(f"{label:26} {value}")

    database.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
