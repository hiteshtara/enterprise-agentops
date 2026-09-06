"""Which opportunities deserve the owner's attention first.

**Decision support, and nothing else.** This module reads rows that
`app.opportunities` already selected and adds three presentation fields:
`priority`, `priority_reasons` and `why_now`, plus the derived
`is_change_clamped` signal. It never touches `proposed_price`, `actionable`,
`blocked_reason`, or anything that decides whether an action may run. A test
holds each of those.

It also computes no price. Every input is a field the opportunity payload
already carried, so there is no second engine here and nothing to drift from
`app.pricing_policy`.

Ranking is triage, not a forecast. It says "look at this one first", never
"this will book" and never a dollar value beyond the nightly uplift the
pricing engine already proposed.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.pricing_config import MAX_CHANGE_PER_RUN


class Priority(str, Enum):
    REVIEW_NOW = "REVIEW_NOW"
    WATCH = "WATCH"
    LOW_PRIORITY = "LOW_PRIORITY"


#: The smallest nightly difference worth interrupting someone for. Below this
#: the owner spends more attention on the decision than the night can return.
REVIEW_NOW_MIN_UPLIFT = 15.0

#: How far a unit's own occupancy must lead its market before that lead counts
#: as evidence in its own right rather than noise between two small samples.
OCCUPANCY_LEAD_POINTS = 15.0

#: At or below this, the night is filed rather than raised. Deliberately below
#: `REVIEW_NOW_MIN_UPLIFT` so there is a middle band -- a real opportunity with
#: a weaker case is WATCH, not dismissed.
LOW_PRIORITY_MAX_UPLIFT = 10.0

#: A percentage gain small enough that it is inside the noise of a nightly
#: rate, whatever the dollar figure.
LOW_PRIORITY_MAX_PCT = 5.0

#: Confidences that can reach REVIEW_NOW. LOW never appears here anyway --
#: `check_guardrails` refuses it long before this module sees a row -- so this
#: is a statement of intent rather than a filter that fires.
REVIEW_NOW_CONFIDENCE = frozenset({"HIGH", "MEDIUM"})

#: Demand labels treated as a stronger environment for the *priority* layer.
#:
#: Note this is deliberately **not** `pricing_recommendations.STRONG_DEMAND`,
#: which counts only "High Demand" and "Good Demand" and files "Normal Demand"
#: under WEAK. The pricing engine is deciding whether to move a price at all,
#: where "normal" is genuinely no evidence; this layer is deciding what a
#: person looks at first, where "not actively soft" is worth something. The two
#: answer different questions, so they are allowed to disagree -- but the
#: disagreement is written down here rather than left to be discovered.
STRONGER_DEMAND = frozenset({"Normal Demand", "Good Demand", "High Demand"})


@dataclass(frozen=True)
class PriorityAssessment:
    """One triage verdict, with the case for it."""

    priority: Priority
    reasons: tuple[str, ...] = field(default=())
    why_now: str = ""
    is_change_clamped: bool = False


def occupancy_lead(row: dict[str, Any]) -> float | None:
    """Percentage points by which this unit's occupancy leads its market."""
    unit = row.get("listing_occupancy")
    market = row.get("market_occupancy")

    if unit is None or market is None:
        return None

    return float(unit) - float(market)


def is_change_clamped(row: dict[str, Any]) -> bool:
    """Whether the proposal was cut short by `MAX_CHANGE_PER_RUN`.

    Two conditions, both required, because either alone is ambiguous:

      * the proposal sits exactly at the per-run ceiling, and
      * the market reference it was aiming at is *above* that ceiling.

    The second is what makes the answer safe. `recommend_night` proposes
    `min(market_p25, current * 1.10)`, so a proposal landing on the ceiling
    when p25 is below it means p25 was the binding constraint and the cap never
    bit. Reporting that as clamped would tell the owner the engine wanted more
    when it did not.

    This is presentation only. Nothing here changes the proposal, and a clamped
    recommendation is not automatically a worse one -- it means the engine
    would have gone further, which is information, not a defect.
    """
    current = row.get("current_price")
    proposed = row.get("proposed_price")
    reference = row.get("market_p25")

    if current is None or proposed is None or reference is None:
        return False

    ceiling = float(current) * (1.0 + MAX_CHANGE_PER_RUN)

    at_ceiling = round(float(proposed)) == round(ceiling)

    return at_ceiling and float(reference) > ceiling


def below_market_p25(row: dict[str, Any]) -> bool:
    """Whether the proposed price still sits at or under the comp set's p25."""
    proposed = row.get("proposed_price")
    reference = row.get("market_p25")

    if proposed is None or reference is None:
        return False

    return float(proposed) <= float(reference)


def _percent(value: Any) -> str:
    return "—" if value is None else f"{float(value):.0f}%"


def _money(value: Any) -> str:
    return "—" if value is None else f"${float(value):,.0f}"


def why_now(row: dict[str, Any]) -> str:
    """One sentence a person can act on, assembled from the row alone.

    Deterministic string building -- no model, no template chosen at random.
    The same row always produces the same sentence, which is what makes it
    quotable in a decision and checkable in a test.

    It states the demand environment, then whatever is actually carrying the
    case: an occupancy lead, headroom to the comp set, or neither.
    """
    demand = row.get("demand")

    opening = f"{demand}" if demand else "Demand unknown"

    lead = occupancy_lead(row)

    parts: list[str] = []

    if lead is not None and lead >= OCCUPANCY_LEAD_POINTS:
        parts.append(
            f"unit occupancy is {_percent(row.get('listing_occupancy'))} "
            f"vs market {_percent(row.get('market_occupancy'))}"
        )

    proposed = row.get("proposed_price")
    reference = row.get("market_p25")

    if reference is not None and proposed is not None:
        # Compare what the reader will see, not the raw floats. A proposal of
        # 600 against a p25 of 599.8 is genuinely above it, but rendering that
        # as "$600 would sit above market p25 of $600" reads as nonsense and
        # costs the sentence its credibility. When the displayed figures are
        # the same number, say they are level -- which is what the reader can
        # verify -- and leave the exact comparison to `below_market_p25`, where
        # it decides the priority band.
        if round(float(proposed)) == round(float(reference)):
            parts.append(
                f"{_money(proposed)} is level with market p25"
            )

        elif below_market_p25(row):
            parts.append(
                f"{_money(proposed)} remains at or below "
                f"market p25 of {_money(reference)}"
            )

        else:
            parts.append(
                f"{_money(proposed)} would sit above "
                f"market p25 of {_money(reference)}"
            )

    if is_change_clamped(row):
        parts.append(
            f"the move is capped at {MAX_CHANGE_PER_RUN:.0%} per run, so the "
            "engine would go further"
        )

    if not parts:
        return f"{opening}; the case rests on the uplift alone."

    return f"{opening}; {', and '.join(parts)}."


def rank_opportunity(row: dict[str, Any]) -> PriorityAssessment:
    """Triage one opportunity. Reads the row; changes nothing.

    The bands, in the order they are tested:

      **LOW_PRIORITY** -- the nightly difference is under
      `LOW_PRIORITY_MAX_UPLIFT`, or the percentage gain is under
      `LOW_PRIORITY_MAX_PCT`. Tested first because a gain this small is not
      worth a decision however good the evidence behind it is.

      **REVIEW_NOW** -- uplift at or above `REVIEW_NOW_MIN_UPLIFT`, confidence
      MEDIUM or HIGH, *and* at least one stronger signal: a demand label that
      is not soft, or an occupancy lead of at least
      `OCCUPANCY_LEAD_POINTS` while the proposed price still sits at or below
      market p25. Uplift alone never reaches REVIEW_NOW -- a big number on no
      evidence is exactly the case that wastes an owner's attention.

      **WATCH** -- everything else: a legitimate opportunity whose case is
      weaker. Soft demand doing the work, a lead below the threshold, a price
      that would cross p25, or evidence that is merely adequate.

    Every band records why, in the order the checks ran, so the verdict can be
    argued with rather than trusted.
    """
    uplift = float(row.get("uplift") or 0.0)
    uplift_pct = float(row.get("uplift_pct") or 0.0)
    confidence = str(row.get("confidence") or "")

    clamped = is_change_clamped(row)
    lead = occupancy_lead(row)
    under_p25 = below_market_p25(row)

    strong_demand = row.get("demand") in STRONGER_DEMAND
    strong_lead = lead is not None and lead >= OCCUPANCY_LEAD_POINTS and under_p25

    def assess(priority: Priority, reasons: list[str]) -> PriorityAssessment:
        return PriorityAssessment(
            priority=priority,
            reasons=tuple(reasons),
            why_now=why_now(row),
            is_change_clamped=clamped,
        )

    if uplift < LOW_PRIORITY_MAX_UPLIFT:
        return assess(
            Priority.LOW_PRIORITY,
            [
                (
                    f"${uplift:,.0f} a night is below the "
                    f"${LOW_PRIORITY_MAX_UPLIFT:,.0f} worth a decision"
                ),
            ],
        )

    if uplift_pct < LOW_PRIORITY_MAX_PCT:
        return assess(
            Priority.LOW_PRIORITY,
            [
                f"{uplift_pct:.1f}% is inside the noise of a nightly rate",
            ],
        )

    if uplift >= REVIEW_NOW_MIN_UPLIFT and confidence in REVIEW_NOW_CONFIDENCE:
        if strong_demand:
            return assess(
                Priority.REVIEW_NOW,
                [
                    f"${uplift:,.0f} a night",
                    f"{row.get('demand')} rather than soft demand",
                    f"{confidence} confidence",
                ],
            )

        if strong_lead:
            return assess(
                Priority.REVIEW_NOW,
                [
                    f"${uplift:,.0f} a night",
                    f"occupancy leads the market by {lead:.0f} points",
                    "the proposed price stays at or below market p25",
                ],
            )

    # Everything that is a real opportunity but has a weaker case.
    reasons = [f"${uplift:,.0f} a night at {confidence} confidence"]

    if not strong_demand:
        reasons.append(f"{row.get('demand') or 'demand unknown'} environment")

    if lead is not None and not strong_lead:
        if lead < OCCUPANCY_LEAD_POINTS:
            reasons.append(f"occupancy lead of {lead:.0f} points is modest")

        elif not under_p25:
            reasons.append("the proposed price would sit above market p25")

    if clamped:
        reasons.append(f"the move is capped at {MAX_CHANGE_PER_RUN:.0%} per run")

    return assess(Priority.WATCH, reasons)


#: Default board order: the band first, then the biggest number inside it.
PRIORITY_ORDER: dict[Priority, int] = {
    Priority.REVIEW_NOW: 0,
    Priority.WATCH: 1,
    Priority.LOW_PRIORITY: 2,
}


def rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the triage fields to every row, and order the board by them.

    Returns new rows rather than mutating: the caller's opportunity list is
    the pricing engine's output, and this layer has no business editing it.
    """
    ranked = []

    for row in rows:
        assessment = rank_opportunity(row)

        ranked.append(
            {
                **row,
                "priority": assessment.priority.value,
                "priority_reasons": list(assessment.reasons),
                "why_now": assessment.why_now,
                "is_change_clamped": assessment.is_change_clamped,
            }
        )

    return sorted(
        ranked,
        key=lambda row: (
            PRIORITY_ORDER[Priority(row["priority"])],
            -float(row["uplift"]),
            row["stay_date"],
        ),
    )


def summarise_priorities(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Counts per band, over exactly the rows shown."""
    return {
        "review_now": sum(1 for r in rows if r["priority"] == Priority.REVIEW_NOW.value),
        "watch": sum(1 for r in rows if r["priority"] == Priority.WATCH.value),
        "low_priority": sum(
            1 for r in rows if r["priority"] == Priority.LOW_PRIORITY.value
        ),
    }
