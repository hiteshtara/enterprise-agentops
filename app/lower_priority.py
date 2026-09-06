"""Triage for LOWER. A different question from RAISE, so a different function.

RAISE asks "where is money being left on the table" and gets more interesting
the larger the gap. LOWER asks "which empty nights am I about to lose", and
that gets more urgent as arrival approaches — a $20 reduction 3 days out is a
last chance; the same reduction 14 days out is a thought.

Reusing the RAISE bands would have inverted that: RAISE ranks on dollars, and
the biggest LOWER dollars sit on the expensive far-out nights that still have
time to sell themselves.

Nothing here decides a price, and nothing here can make a blocked action
runnable. Every row was already produced, guardrailed and gated by the pricing
engine; this only orders what a person looks at first.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.opportunity_priority import Priority

#: Inside this many days, an unsold night is running out of chances. Matches
#: `pricing_recommendations.NEAR_TERM_DAYS`, which is the same judgement made
#: for the same reason -- imported rather than restated so they cannot drift.
from app.pricing_recommendations import NEAR_TERM_DAYS

#: Days out at which an open night becomes urgent rather than merely watched.
#: Deliberately tighter than `NEAR_TERM_DAYS`: the engine will *consider* a
#: reduction for two weeks out, but that is not the same as asking a person to
#: decide today.
REVIEW_NOW_DAYS_OUT = 7

#: How far above its own lead-band history a night must be priced before the
#: gap is worth acting on rather than noting. Below this the reduction is
#: fine-tuning, not vacancy-filling.
MATERIAL_GAP_PCT = 15.0

#: The engine already requires three samples to form a median. A band sitting
#: exactly on that minimum is thin enough that the case is worth watching
#: rather than acting on.
CONFIDENT_SAMPLE_COUNT = 5


@dataclass(frozen=True)
class LowerAssessment:
    priority: Priority
    reasons: tuple[str, ...] = field(default=())
    why_now: str = ""


class LowerFlag(str, Enum):
    """Conditions a reviewer should see named rather than infer."""

    BELOW_OWNER_FLOOR = "BELOW_OWNER_FLOOR"
    THIN_HISTORY = "THIN_HISTORY"
    SIGNAL_CONFLICT = "SIGNAL_CONFLICT"


def flags(row: dict[str, Any]) -> tuple[str, ...]:
    """What is unusual about this reduction, in a fixed order."""
    found = []

    if row.get("below_owner_floor"):
        found.append(LowerFlag.BELOW_OWNER_FLOOR.value)

    if int(row.get("history_sample_count") or 0) < CONFIDENT_SAMPLE_COUNT:
        found.append(LowerFlag.THIN_HISTORY.value)

    if row.get("market_signal_conflict"):
        found.append(LowerFlag.SIGNAL_CONFLICT.value)

    return tuple(found)


def rank_lower(row: dict[str, Any]) -> LowerAssessment:
    """Triage one LOWER. Reads the row; changes nothing.

    **REVIEW_NOW** wants all of: close to arrival, a materially above-history
    price, a history sample worth trusting, a proposal at or above the owner
    floor, and no conflicting market signal. Any one missing is WATCH.

    **WATCH** is the default rather than the leftovers. A reduction that is
    farther out, resting on three bookings, crossing the owner floor, or
    contradicted by the comp set is a real candidate whose case a person
    should weigh — not one to act on before breakfast.

    There is no LOW_PRIORITY band. Every LOWER on the board is an open night
    the engine believes is overpriced against this property's own history;
    "file this away" is not a useful thing to say about that. RAISE has the
    band because a $6 uplift genuinely is not worth a decision.
    """
    days_out = int(row.get("days_out") or 0)
    gap_pct = float(row.get("historical_reference_gap_pct") or 0.0)
    samples = int(row.get("history_sample_count") or 0)

    row_flags = flags(row)

    near = days_out <= REVIEW_NOW_DAYS_OUT
    material = gap_pct >= MATERIAL_GAP_PCT
    confident = samples >= CONFIDENT_SAMPLE_COUNT

    reasons: list[str] = [f"{days_out}d to arrival and still open"]

    if material:
        reasons.append(
            f"asking {gap_pct:.0f}% above what this property converts at "
            f"in this lead band (n={samples})"
        )

    else:
        reasons.append(
            f"only {gap_pct:.0f}% above its lead-band history (n={samples})"
        )

    if not near:
        reasons.append(
            f"still {days_out}d out, so the night has time to sell itself"
        )

    if not confident:
        reasons.append(f"the band rests on {samples} bookings")

    if LowerFlag.BELOW_OWNER_FLOOR.value in row_flags:
        reasons.append("the proposal is below the owner floor")

    if LowerFlag.SIGNAL_CONFLICT.value in row_flags:
        reasons.append("the market comparison argues the other way")

    urgent = near and material and confident and not row_flags

    return LowerAssessment(
        priority=Priority.REVIEW_NOW if urgent else Priority.WATCH,
        reasons=tuple(reasons),
        why_now=why_now(row),
    )


def why_now(row: dict[str, Any]) -> str:
    """One deterministic sentence. No model, no template chosen at random."""
    days_out = row.get("days_out")
    demand = row.get("demand") or "Demand unknown"
    gap_pct = row.get("historical_reference_gap_pct")
    adr = row.get("historical_lead_band_adr")
    samples = row.get("history_sample_count")

    parts = [f"{days_out}d to arrival, {demand.lower()}"]

    if adr and gap_pct is not None:
        parts.append(
            f"asking ${float(row['current_price']):,.0f} against the "
            f"${float(adr):,.0f} this property has converted at in this lead "
            f"band (n={samples}) — {gap_pct:.0f}% above"
        )

    if row.get("below_owner_floor"):
        parts.append(
            "the proposal is below the owner floor, so this needs an explicit "
            "vacancy decision"
        )

    return "; ".join(parts) + "."


def rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add triage fields and order the LOWER board.

    Sorted by band, then by *proximity to arrival* rather than by dollars.
    The night that runs out of time first is the one to look at first.
    """
    ranked = []

    for row in rows:
        assessment = rank_lower(row)

        ranked.append(
            {
                **row,
                "priority": assessment.priority.value,
                "priority_reasons": list(assessment.reasons),
                "why_now": assessment.why_now,
                "lower_flags": list(flags(row)),
            }
        )

    return sorted(
        ranked,
        key=lambda row: (
            0 if row["priority"] == Priority.REVIEW_NOW.value else 1,
            int(row.get("days_out") or 0),
            row["stay_date"],
        ),
    )


def summarise_lower(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts over exactly the rows shown.

    `total_reduction` is the sum of the per-night price differences below.
    It is not lost revenue and not expected revenue: these nights are unsold,
    so there is no revenue to lose, and lowering a price is not a booking.
    """
    return {
        "opportunities": len(rows),
        "review_now": sum(
            1 for r in rows if r["priority"] == Priority.REVIEW_NOW.value
        ),
        "watch": sum(1 for r in rows if r["priority"] == Priority.WATCH.value),
        "below_owner_floor": sum(1 for r in rows if r.get("below_owner_floor")),
        "market_signal_conflict": sum(
            1 for r in rows if r.get("market_signal_conflict")
        ),
        "total_reduction": round(
            sum(
                float(r["current_price"]) - float(r["proposed_price"])
                for r in rows
                if r.get("current_price") and r.get("proposed_price")
            ),
            2,
        ),
    }


def select_lower(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The LOWER rows a person may act on, from the full recommendation set.

    Mirrors `app.opportunities.select` deliberately: actionable, not blocked
    by a verification gate, not stale. A LOWER that the guardrails refused, or
    that the channel gate still blocks, is not something to put in front of
    someone as a decision.
    """
    return [
        row
        for row in rows
        if row.get("action") == "LOWER"
        and row.get("actionable")
        and not row.get("blocked_reason")
        and not row.get("stale")
        and row.get("current_price")
        and row.get("proposed_price")
    ]


__all__ = [
    "CONFIDENT_SAMPLE_COUNT",
    "MATERIAL_GAP_PCT",
    "NEAR_TERM_DAYS",
    "REVIEW_NOW_DAYS_OUT",
    "LowerAssessment",
    "LowerFlag",
    "flags",
    "rank",
    "rank_lower",
    "select_lower",
    "summarise_lower",
    "why_now",
]
