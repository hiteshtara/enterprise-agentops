"""Deterministic pricing recommendations and the guardrails they must pass.

No language model participates. The same PriceLabs state always produces the
same recommendation, and every refusal names the rule that produced it.

Three actions can become a write: `LOWER`, `RAISE`, `REMOVE_PIN`. `HOLD` and
`KEEP_PIN` are informational and are structurally incapable of becoming one --
`Recommendation.is_actionable` is what the route filters on, and only an
actionable recommendation is ever submitted for approval.

The guardrail order matters and is asserted by tests:

  1. the listing must have owner-approved bands at all (no bands, no write);
  2. the move must be within `MAX_CHANGE_PER_RUN`;
  3. a LOWER must land at or above the hard floor -- never below, ever;
  4. a RAISE must land at or below the auto-raise ceiling, and never above the
     absolute ceiling;
  5. a listing flagged `raise_requires_human` yields to a person on any RAISE.

A desired move larger than the cap is *not* split across runs. The next
recommendation is computed from freshly re-read market state, so a 25% gap
closes over several days of real evidence rather than three chained executions
against one stale reading.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from app.pricing_config import MAX_CHANGE_PER_RUN, PricingBands, unverified_reason


class PriceAction(str, Enum):
    LOWER = "LOWER"
    RAISE = "RAISE"
    REMOVE_PIN = "REMOVE_PIN"
    HOLD = "HOLD"
    KEEP_PIN = "KEEP_PIN"


#: The only actions that may ever result in a PriceLabs write.
WRITE_ACTIONS: frozenset[PriceAction] = frozenset(
    {PriceAction.LOWER, PriceAction.RAISE, PriceAction.REMOVE_PIN}
)


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Refusal(str, Enum):
    """Why a candidate did not become an actionable recommendation."""

    NO_BANDS = "no owner-approved bands for this listing"
    BELOW_HARD_FLOOR = "proposed price is below the hard floor"
    ABOVE_AUTO_CEILING = "proposed price is above the auto-raise ceiling"
    ABOVE_ABSOLUTE_CEILING = "proposed price is above the absolute ceiling"
    EXCEEDS_MAX_CHANGE = "move exceeds the maximum change per run"
    LOW_CONFIDENCE = "confidence is LOW"
    NO_CHANGE = "proposed price equals the current price"


@dataclass(frozen=True)
class MarketState:
    """The PriceLabs readings a recommendation was computed from.

    Everything here goes into the fingerprint. If any of it moves materially
    before execution, the recommendation is stale and must not be applied.
    """

    current_price: float | None
    market_p25: float | None
    market_booked_median: float | None
    market_occupancy: float | None
    listing_occupancy: float | None
    demand: str | None
    pickup_7_days: float | None
    pinned_price: float | None
    last_refreshed_at: str | None


@dataclass(frozen=True)
class Recommendation:
    """One proposed change, with the whole case for it."""

    listing_id: str
    slug: str
    display_name: str
    stay_date: date
    days_out: int
    action: PriceAction
    current_price: float | None
    proposed_price: float | None
    confidence: Confidence
    reason: str
    state: MarketState
    bands: PricingBands | None
    #: Set when a candidate was downgraded to HOLD/KEEP_PIN by a guardrail.
    refused: Refusal | None = None
    #: True when this listing's own switch forces a person to decide a RAISE.
    requires_human: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def is_actionable(self) -> bool:
        return self.action in WRITE_ACTIONS

    @property
    def pct_change(self) -> float | None:
        if not self.current_price or self.proposed_price is None:
            return None

        return (self.proposed_price - self.current_price) / self.current_price

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.listing_id, self.stay_date, self.state)


def fingerprint(listing_id: str, stay_date: date, state: MarketState) -> str:
    """A deterministic hash of the state a recommendation was computed from.

    Rounded before hashing so a trivial repricing does not invalidate a
    recommendation, while any move a person would notice does. What counts as
    material is therefore a property of this function, not of the caller.
    """
    payload = {
        "listing_id": listing_id,
        "stay_date": stay_date.isoformat(),
        "current_price": _round(state.current_price),
        "market_p25": _round(state.market_p25),
        "market_booked_median": _round(state.market_booked_median),
        "pinned_price": _round(state.pinned_price),
        "demand": state.demand,
    }

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value))


def clamp_move(current: float, proposed: float) -> float:
    """Pull `proposed` inside the per-run cap, keeping its direction."""
    cap = current * MAX_CHANGE_PER_RUN

    if proposed > current:
        return min(proposed, current + cap)

    return max(proposed, current - cap)


def check_guardrails(
    action: PriceAction,
    current: float | None,
    proposed: float | None,
    bands: PricingBands | None,
    confidence: Confidence,
) -> Refusal | None:
    """The single gate every write action passes. None means it may proceed.

    `REMOVE_PIN` sets no price -- it hands the date back to PriceLabs -- so the
    floor and ceiling checks do not apply to it. It still requires bands,
    because a listing without owner sign-off is not under automation at all.
    """
    if bands is None:
        return Refusal.NO_BANDS

    if confidence is Confidence.LOW:
        return Refusal.LOW_CONFIDENCE

    if action is PriceAction.REMOVE_PIN:
        return None

    if current is None or proposed is None:
        return Refusal.NO_CHANGE

    if round(proposed) == round(current):
        return Refusal.NO_CHANGE

    if abs(proposed - current) / current > MAX_CHANGE_PER_RUN + 1e-9:
        return Refusal.EXCEEDS_MAX_CHANGE

    if action is PriceAction.LOWER and proposed < bands.hard_floor:
        return Refusal.BELOW_HARD_FLOOR

    if action is PriceAction.RAISE:
        if proposed > bands.absolute_ceiling:
            return Refusal.ABOVE_ABSOLUTE_CEILING

        if proposed > bands.auto_raise_ceiling:
            return Refusal.ABOVE_AUTO_CEILING

    return None


def below_normal_floor(
    action: PriceAction,
    proposed: float | None,
    bands: PricingBands | None,
) -> bool:
    """A LOWER between the hard and normal floors. Allowed, but not routine.

    It clears the hard floor so it is not refused, and it sits under the
    preferred minimum, so it is surfaced for an explicit human decision rather
    than treated as an ordinary adjustment.
    """
    if action is not PriceAction.LOWER or bands is None or proposed is None:
        return False

    return bands.hard_floor <= proposed < bands.normal_floor


def finalise(
    candidate: Recommendation,
) -> Recommendation:
    """Apply the guardrails, downgrading to HOLD/KEEP_PIN when they refuse.

    A refused candidate keeps its evidence and its reason -- the console shows
    why a change was considered and why it will not be offered -- but it is no
    longer actionable, so no route can submit it for approval.
    """
    if candidate.action not in WRITE_ACTIONS:
        return candidate

    refusal = check_guardrails(
        candidate.action,
        candidate.current_price,
        candidate.proposed_price,
        candidate.bands,
        candidate.confidence,
    )

    if refusal is None:
        needs_human = bool(
            candidate.bands
            and candidate.action is PriceAction.RAISE
            and candidate.bands.raise_requires_human
        )

        notes = candidate.notes

        if needs_human:
            notes = notes + (
                (
                    "RAISE on this listing always requires a human decision: "
                    "its comp set has not been validated."
                ),
            )

        if below_normal_floor(
            candidate.action,
            candidate.proposed_price,
            candidate.bands,
        ):
            notes = notes + (
                (
                    "Below the normal floor but above the hard floor -- needs "
                    "an explicit decision and a near-term vacancy reason."
                ),
            )

        return Recommendation(
            **{
                **asdict_shallow(candidate),
                "requires_human": needs_human,
                "notes": notes,
            }
        )

    downgraded = (
        PriceAction.KEEP_PIN
        if candidate.action is PriceAction.REMOVE_PIN
        else PriceAction.HOLD
    )

    return Recommendation(
        **{
            **asdict_shallow(candidate),
            "action": downgraded,
            "proposed_price": candidate.current_price,
            "refused": refusal,
            "reason": f"{candidate.reason} — blocked: {refusal.value}",
        }
    )


def asdict_shallow(rec: Recommendation) -> dict:
    """Field-by-field copy that keeps `state` and `bands` as objects."""
    data = {key: getattr(rec, key) for key in rec.__dataclass_fields__}

    return data


def to_payload(rec: Recommendation) -> dict:
    """The console/API projection. Carries evidence, never a credential."""
    return {
        "id": f"{rec.listing_id}:{rec.stay_date.isoformat()}",
        "listing_id": rec.listing_id,
        "slug": rec.slug,
        "display_name": rec.display_name,
        "stay_date": rec.stay_date.isoformat(),
        "days_out": rec.days_out,
        "action": rec.action.value,
        "current_price": rec.current_price,
        "proposed_price": rec.proposed_price,
        "pct_change": (
            None if rec.pct_change is None else round(rec.pct_change * 100, 1)
        ),
        "confidence": rec.confidence.value,
        "reason": rec.reason,
        "refused": rec.refused.value if rec.refused else None,
        "requires_human": rec.requires_human,
        "notes": list(rec.notes),
        "fingerprint": rec.fingerprint,
        "actionable": rec.is_actionable,
        # Why this action cannot execute yet, even once approved. Surfaced so
        # the console can say so before a person spends a decision on it.
        "blocked_reason": (
            unverified_reason(rec.action.value) if rec.is_actionable else None
        ),
        "pricelabs_minimum": None,
        "hard_floor": rec.bands.hard_floor if rec.bands else None,
        "normal_floor": rec.bands.normal_floor if rec.bands else None,
        "auto_raise_ceiling": rec.bands.auto_raise_ceiling if rec.bands else None,
        "absolute_ceiling": rec.bands.absolute_ceiling if rec.bands else None,
        "market_p25": rec.state.market_p25,
        "market_booked_median": rec.state.market_booked_median,
        "market_occupancy": rec.state.market_occupancy,
        "listing_occupancy": rec.state.listing_occupancy,
        "demand": rec.state.demand,
        "pickup_7_days": rec.state.pickup_7_days,
        "pinned_price": rec.state.pinned_price,
        "last_refreshed_at": rec.state.last_refreshed_at,
    }
