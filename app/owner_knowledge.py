"""Rules the owner wrote, seeded into the knowledge store as approved.

Distilled knowledge is *proposed* because a model guessed at it from old
messages. These did not come from old messages: the owner stated them directly,
so they arrive APPROVED under the same manual-authoring semantics as a rule
typed into the Knowledge console. `create_manual` is the same code path; this
module only supplies the text.

Seeding is explicit and idempotent, like every other seed in this project --
nothing runs on import, and re-running skips what already exists rather than
overwriting an edit the owner has since made in the console.

Keep the wording here in step with the deterministic policy modules it
describes -- `app/late_checkout.py`, `app/early_check_in.py` and
`app/cancellation.py`. Those enforce the numbers; this is what the model reads
and what the owner can edit.
"""

import logging
from dataclasses import dataclass

from app.knowledge import GLOBAL_SCOPE, KnowledgeStore
from app.knowledge_topics import GUEST_FACING

logger = logging.getLogger(__name__)

SEED_ACTOR = "owner-authored"


@dataclass(frozen=True)
class OwnerRule:
    topic: str
    title: str
    content: str


OWNER_RULES: tuple[OwnerRule, ...] = (
    OwnerRule(
        topic="late_checkout",
        title="Late checkout may be offered until 11:00 AM",
        content=(
            "Standard checkout is 10:00 AM. When a guest asks about a later "
            "checkout, offer 11:00 AM -- that extra hour is already approved "
            "and does not need to be checked with anyone. 11:00 AM is also the "
            "latest that may ever be offered or confirmed automatically. If a "
            "guest asks for a later time than 11:00 AM, do not refuse and do "
            "not agree: offer 11:00 AM as what is available now, and say that "
            "anything later has to be checked first. Never volunteer a longer "
            "extension than the guest asked for."
        ),
    ),
    OwnerRule(
        topic="early_check_in",
        title="Early check-in is never promised automatically",
        content=(
            "Standard check-in is 4:00 PM. Early check-in is never promised "
            "just because a guest asks, and the earlier time is never chosen "
            "by the assistant. Whether it is possible depends on whether "
            "another guest is checking out of the property on the arrival day. "
            "If someone is checking out that day, say the turnover needs the "
            "full day and that check-in is at 4:00 PM. If nobody is, say early "
            "check-in may be possible and that the schedule will be confirmed "
            "-- never that it is agreed. If the schedule cannot be checked, say "
            "so; an unchecked schedule is never 'there is no checkout'. Far "
            "ahead of arrival, say it depends on that day's checkout and will "
            "be confirmed nearer the time."
        ),
    ),
    OwnerRule(
        topic="cancellation_retention",
        title="Cancelled reservations get a 30% retention offer",
        content=(
            "When a reservation has actually been cancelled, say that Priyanka "
            "Homes values the guest's business and would like to keep it: offer "
            "30% off if they keep or restore the current reservation, and say "
            "the same 30% can be applied to their next Boston visit if they "
            "still need to cancel. The figure is 30% and never any other "
            "number. Make the offer only when the reservation really is "
            "cancelled -- asking about the cancellation policy, saying they "
            "might cancel, asking about a refund, or wanting to change or "
            "shorten dates is not a cancellation. Do not make the offer twice "
            "in the same conversation. Nothing about how the discount works has "
            "been decided: there is no coupon code, expiry date, blackout list "
            "or stacking rule, and no decision about taxes or channel fees, so "
            "never describe any of those, and never apply the discount -- a "
            "person does that."
        ),
    ),
)


def seed_owner_knowledge(
    store: KnowledgeStore,
    actor_user_id: str = SEED_ACTOR,
) -> list[str]:
    """Insert any owner-authored rule the store does not already hold.

    Returns the topics actually inserted. A rule already present is left exactly
    as it is: the owner may have refined the wording in the console since, and
    a seed run must never quietly revert that.
    """
    added: list[str] = []

    for rule in OWNER_RULES:
        try:
            store.create_manual(
                property_slug=None,
                topic=rule.topic,
                title=rule.title,
                content=rule.content,
                audience=GUEST_FACING,
                actor_user_id=actor_user_id,
            )

        except ValueError:
            # Already there. `create_manual` refuses a duplicate scope+topic+
            # title, which is exactly the idempotency this needs.
            logger.info("owner rule already present: %s/%s", GLOBAL_SCOPE, rule.topic)

            continue

        added.append(rule.topic)

    return added


if __name__ == "__main__":  # pragma: no cover
    inserted = seed_owner_knowledge(KnowledgeStore())

    print(f"owner rules inserted: {inserted or 'none (all already present)'}")
