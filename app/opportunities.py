"""The 60-day revenue-opportunity view. Read-only, and derived, not decided.

This module computes nothing about pricing. Every recommendation it handles was
already produced by `PricingRecommendationService` and the deterministic rules
in `app.pricing_policy`; all that happens here is *selection* -- which of those
recommendations a person should look at -- and arithmetic over what is
selected. Adding a pricing rule here would mean two engines that could
disagree, which is the one thing this file must not become.

What counts as an opportunity
-----------------------------
A RAISE that has already cleared every deterministic guardrail, is not blocked
by a verification gate, and rests on evidence current enough to act on. That is
deliberately narrower than "a night where we might charge more":

  * HOLD and KEEP_PIN are not opportunities. They are the engine saying no.
  * LOWER is never an opportunity. It stays blocked by the Booking.com
    exposure gate, and presenting a blocked action as something to act on
    would misrepresent what the system will let anyone do.
  * A refused RAISE -- over a ceiling, past the per-run cap, LOW confidence --
    is not an opportunity either. `is_actionable` is already false for those.
  * A booked or unbookable night is not open inventory, and the engine has
    already returned HOLD for it.
  * A stale reading is not evidence. See `pricing_policy.is_stale`.

Uplift is a price difference, not revenue
-----------------------------------------
`uplift` is `proposed_price - current_price` for one night, and the summary
total is the sum of those differences. It is **not** expected revenue and must
never be presented as such: it assumes every night books, at the higher price,
which is exactly the assumption the whole vacancy feature exists to question.
"""

from typing import Any

#: The one action that can be an opportunity.
OPPORTUNITY_ACTION = "RAISE"


def is_opportunity(row: dict[str, Any]) -> bool:
    """Whether one recommendation payload is something to act on.

    Every condition is read off the payload the engine already produced. The
    checks are `and`-ed rather than short-circuited into a single expression so
    a reader can see each reason on its own line.
    """
    if row.get("action") != OPPORTUNITY_ACTION:
        return False

    # The engine's own verdict on whether this could become a write at all.
    if not row.get("actionable"):
        return False

    # Blocked by a verification gate: permitted to exist, not permitted to run.
    if row.get("blocked_reason"):
        return False

    if row.get("stale"):
        return False

    return uplift_of(row) is not None


def uplift_of(row: dict[str, Any]) -> float | None:
    """`proposed - current` for one night, or None when it is not computable.

    A RAISE with no proposed price, or no current price, has no uplift to show
    and is not presented -- rather than being shown as $0, which would read as
    "no gain" instead of "not known".
    """
    current = row.get("current_price")
    proposed = row.get("proposed_price")

    if current is None or proposed is None:
        return None

    difference = float(proposed) - float(current)

    return difference if difference > 0 else None


def select(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The opportunities, richest first.

    Default order is dollar uplift descending, because the question this view
    answers is "where is the most money on the table tonight". Ties break by
    stay date so the order is stable rather than incidental.
    """
    chosen = []

    for row in rows:
        if not is_opportunity(row):
            continue

        uplift = uplift_of(row)

        chosen.append(
            {
                **row,
                "uplift": round(uplift, 2),
                "uplift_pct": round(100.0 * uplift / float(row["current_price"]), 1),
            }
        )

    return sorted(
        chosen,
        key=lambda row: (-row["uplift"], row["stay_date"]),
    )


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline counts over the selected opportunities.

    `total_uplift` is the exact sum of the per-night differences shown below
    it, so a reader can add the column up and get the same number. It is a
    price difference, not a revenue forecast.
    """
    return {
        "opportunities": len(rows),
        "total_uplift": round(sum(row["uplift"] for row in rows), 2),
        "high_confidence": sum(1 for row in rows if row["confidence"] == "HIGH"),
        "medium_confidence": sum(1 for row in rows if row["confidence"] == "MEDIUM"),
        "properties": len({row["listing_id"] for row in rows}),
    }
