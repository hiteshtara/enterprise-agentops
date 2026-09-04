"""The one pricing capability the governance layer can execute.

Exactly one tool, and it is not model-callable. `apply_pricing_action` is
registered with `ToolRisk.DANGEROUS` and `model_callable=False`, so:

  * the model is never told it exists -- `ToolRegistry.definitions()` omits it,
    and a name the model invents is rejected as an unknown tool;
  * the console still sees it in `ToolRegistry.describe()`, because an operator
    reviewing what this deployment can do must see every capability; and
  * `ToolRegistry.execute()` still refuses to run it without a recorded human
    approval.

The division of labour: Python computes the recommendation, a person approves
one specific change, and Python carries it out. There is no path by which a
model can price a night.

Staleness
---------
A recommendation is computed from a reading of PriceLabs. Between that reading
and the approval, the price can move, the override can change, or the market
can shift. So the fingerprint of the state used to build the recommendation
travels with it, and this tool recomputes that fingerprint from a fresh read
before writing. If it differs, the action is refused as STALE and nothing is
sent. Yesterday's recommendation is never executed against today's market.
"""

from datetime import UTC, datetime
from typing import Any

from app.connectors.pricelabs.client import PriceLabsClient
from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.connectors.pricelabs.normalise import parse_market_series
from app.connectors.pricelabs.write_client import (
    PriceLabsWriteClient,
    PricingWritesDisabled,
    WriteOutcome,
)
from app.pricing_config import bands_for, unverified_reason
from app.pricing_policy import MarketState, PriceAction, fingerprint

APPLY_PRICING_ACTION_TOOL = "apply_pricing_action"

#: PriceLabs mirrors a PMS on a sync cycle. Older than this and the reading is
#: not a basis for changing a price.
MAX_DATA_AGE_HOURS = 24

APPLY_PRICING_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "listing_id": {"type": "string"},
        "stay_date": {"type": "string"},
        "action": {"type": "string", "enum": ["LOWER", "RAISE", "REMOVE_PIN"]},
        "proposed_price": {"type": ["number", "null"]},
        "fingerprint": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["listing_id", "stay_date", "action", "fingerprint", "reason"],
    "additionalProperties": False,
}


class PriceLabsPricingTools:
    """Executes one approved pricing action against PriceLabs."""

    def __init__(
        self,
        reader: PriceLabsClient,
        writer: PriceLabsWriteClient,
        pms: str = "lodgify",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._pms = pms

    def _current_state(
        self,
        listing_id: str,
        stay_date: str,
    ) -> tuple[MarketState, str]:
        """Re-read the state this decision depends on, right now."""
        listings = {row.get("id"): row for row in self._reader.listings()}

        listing = listings.get(listing_id)

        if listing is None:
            raise PriceLabsUnavailable("Listing is no longer present")

        priced = self._reader.listing_prices(
            [(listing_id, self._pms)],
            stay_date,
            stay_date,
        )

        night: dict[str, Any] = {}

        refreshed = None

        for entry in priced:
            refreshed = entry.get("last_refreshed_at")

            for row in entry.get("data") or []:
                if row.get("date") == stay_date:
                    night = row

        override = None

        for row in self._reader.overrides(listing_id, self._pms):
            if row.get("date") == stay_date:
                override = row

        # The market reference is part of the state a recommendation was made
        # from, so it has to be re-read here too. Leaving it out made the
        # execution-time fingerprint structurally unable to match the one
        # computed when the recommendation was built, which refused every
        # action as STALE -- a write path that could never fire.
        bedrooms = listing.get("no_of_bedrooms")

        reference: dict[str, float | None] = {}

        try:
            reference = parse_market_series(
                self._reader.neighborhood_data(listing_id, self._pms),
                bedrooms if isinstance(bedrooms, int) else None,
            ).get(stay_date, {})

        except PriceLabsUnavailable:
            # Unknown market state is not "no market state": leaving these None
            # would silently change the fingerprint and refuse the action,
            # which is the safe direction, so let it happen rather than guess.
            reference = {}

        def number(value: Any) -> float | None:
            try:
                out = float(value)

            except (TypeError, ValueError):
                return None

            return out if out > 0 else None

        state = MarketState(
            current_price=number(night.get("price")),
            market_p25=reference.get("p25"),
            market_booked_median=reference.get("booked_median"),
            market_occupancy=reference.get("market_occupancy"),
            listing_occupancy=None,
            demand=night.get("demand_desc"),
            pickup_7_days=None,
            pinned_price=number(override.get("price")) if override else None,
            last_refreshed_at=refreshed,
        )

        return state, str(listing.get("currency") or "USD")

    def apply_pricing_action(
        self,
        listing_id: str,
        stay_date: str,
        action: str,
        fingerprint: str,
        reason: str,
        proposed_price: float | None = None,
    ) -> dict[str, Any]:
        """Apply one approved action. Never retries, never loops."""
        bands = bands_for(listing_id)

        if bands is None:
            return _refused(
                "NO_BANDS",
                "This listing has no owner-approved pricing bands.",
                stay_date,
            )

        try:
            parsed = PriceAction(action)

        except ValueError:
            return _refused("INVALID_ACTION", f"Unknown action: {action}", stay_date)

        # Checked before anything is read, let alone written: an action whose
        # provider behaviour is still unproven does not get to run just because
        # a person approved it. Approval authorises *this change*; it cannot
        # authorise an assumption nobody has tested.
        blocked = unverified_reason(parsed.value)

        if blocked is not None:
            return _refused("UNVERIFIED_BEHAVIOUR", blocked, stay_date)

        try:
            state, currency = self._current_state(listing_id, stay_date)

        except PriceLabsUnavailable:
            return _refused(
                "PROVIDER_UNAVAILABLE",
                "PriceLabs could not be read, so nothing was changed.",
                stay_date,
            )

        age = _age_hours(state.last_refreshed_at)

        if age is None or age > MAX_DATA_AGE_HOURS:
            return _refused(
                "STALE_DATA",
                (
                    "PriceLabs data is too old to price against "
                    f"({'unknown age' if age is None else f'{age:.0f}h'}); "
                    "nothing was changed."
                ),
                stay_date,
            )


        current = fingerprint_of(listing_id, stay_date, state)

        if current != fingerprint:
            return _refused(
                "STALE",
                (
                    "PriceLabs state changed after this recommendation was "
                    "made; nothing was changed. Recompute and review again."
                ),
                stay_date,
            )

        try:
            if parsed is PriceAction.REMOVE_PIN:
                result = self._writer.remove_override(
                    listing_id,
                    self._pms,
                    stay_date,
                    automation_enabled=bands.automation_enabled,
                )

            else:
                if proposed_price is None:
                    return _refused(
                        "NO_PRICE",
                        "No proposed price was supplied.",
                        stay_date,
                    )

                result = self._writer.set_override(
                    listing_id,
                    self._pms,
                    stay_date,
                    float(proposed_price),
                    currency=currency,
                    reason=reason,
                    automation_enabled=bands.automation_enabled,
                )

        except PricingWritesDisabled as exc:
            return _refused("WRITES_DISABLED", str(exc), stay_date)

        except PriceLabsUnavailable:
            return {
                "outcome": WriteOutcome.UNKNOWN_WRITE_STATE.value,
                "stay_date": stay_date,
                "message": (
                    "PriceLabs did not answer. The change may already be live. "
                    "Check PriceLabs before doing anything else — do not retry."
                ),
                "needs_human": True,
            }

        return {
            "outcome": result.outcome.value,
            "stay_date": result.stay_date,
            "old_price": result.old_price,
            "new_price": result.new_price,
            "message": result.message,
            "needs_human": result.needs_human,
        }


def fingerprint_of(listing_id: str, stay_date: str, state: MarketState) -> str:
    from datetime import date as _date

    return fingerprint(listing_id, _date.fromisoformat(stay_date), state)


def _refused(code: str, message: str, stay_date: str) -> dict[str, Any]:
    """A refusal is a clean outcome: nothing was sent, nothing changed."""
    return {
        "outcome": WriteOutcome.CONFIRMED_FAILED.value,
        "refusal": code,
        "stay_date": stay_date,
        "message": message,
        "needs_human": False,
    }


def _age_hours(stamp: str | None) -> float | None:
    if not stamp:
        return None

    try:
        when = datetime.fromisoformat(stamp)

    except ValueError:
        return None

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    return (datetime.now(UTC) - when).total_seconds() / 3600.0
