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

Refusal order
-------------
Not every refusal can be reached without touching the provider, and the
distinction is worth stating precisely rather than claiming more than is true.

*Locally decidable* refusals -- owner bands, the action name, the verification
gates, and both runtime kill switches -- are settled from configuration alone.
They all run first: before any provider access, before the credential is
resolved, and before a cleanup row is created. An action refused for one of
these reasons does nothing whatsoever.

*Later* refusals -- STALE, STALE_DATA, PROVIDER_UNAVAILABLE -- follow read-only
provider access, and could not be reached any other way. Whether the market
moved since the recommendation, and how old the provider's data is, are only
knowable from fresh provider state; there is no deciding them without asking.

What holds across both: **no refusal may leave behind a cleanup obligation for
a write that was never attempted.** The cleanup row is created immediately
before the write and after every check that could refuse, so a refusal never
produces a record of an obligation that does not exist.

Staleness
---------
A recommendation is computed from a reading of PriceLabs. Between that reading
and the approval, the price can move, the override can change, or the market
can shift. So the fingerprint of the state used to build the recommendation
travels with it, and this tool recomputes that fingerprint from a fresh read
before writing. If it differs, the action is refused as STALE and nothing is
sent. Yesterday's recommendation is never executed against today's market.
"""

import datetime as _dt
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
from app.pricing_cleanup import (
    CleanupState,
    PricingCleanupStore,
    build_reason,
    default_cleanup_at,
)
from app.pricing_config import bands_for, unverified_reason, writes_enabled
from app.pricing_policy import (
    MAX_DATA_AGE_HOURS as _MAX_DATA_AGE_HOURS,
)
from app.pricing_policy import (
    MarketState,
    PriceAction,
    fingerprint,
)
from app.tool_registry import ExecutionContext

APPLY_PRICING_ACTION_TOOL = "apply_pricing_action"

#: Re-exported from `app.pricing_policy`, which owns it: the write path and
#: the opportunity view must agree on what "too old to act on" means, and two
#: constants would eventually disagree.
MAX_DATA_AGE_HOURS = _MAX_DATA_AGE_HOURS

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
        cleanups: PricingCleanupStore | None = None,
        outcomes: Any = None,
        evidence_source: Any = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._pms = pms
        self._cleanups = cleanups
        # Outcome tracking is observational and entirely optional. Both of
        # these absent means no row is recorded and the write path behaves
        # exactly as it did before -- the tracker is never a dependency of
        # changing a price.
        self._outcomes = outcomes
        self._evidence_source = evidence_source

    def _record_outcome(
        self,
        *,
        ctx: ExecutionContext,
        listing_id: str,
        stay_date: str,
        action: str,
        write_outcome: str,
        state: MarketState,
        currency: str,
        proposed_price: float | None,
        cleanup_id: str | None,
    ) -> None:
        """Record what we just did, for later observation. Never raises.

        **Analytics may not fail a pricing action.** The price has already
        moved at the provider by the time this runs; an exception here would
        turn a successful, irreversible write into a reported failure, and
        the one thing worse than losing a row of analytics is a caller
        believing a live price change did not happen. So every failure is
        swallowed and surfaced through the reconciler-health view instead,
        where a stalled or failing tracker is visible without being confused
        for "nothing booked".

        Evidence is derived **server-side** here. It is never taken from the
        tool's arguments: a caller supplying the market data its own decision
        is later judged against is not evidence, it is assertion.
        """
        if self._outcomes is None:
            return

        try:
            evidence: dict[str, Any] = {}

            if self._evidence_source is not None:
                evidence = self._evidence_source(listing_id, stay_date) or {}

            bands = bands_for(listing_id)

            evidence = {
                "history_adr": evidence.get("historical_lead_band_adr"),
                "history_sample_count": evidence.get("history_sample_count"),
                "market_p25": state.market_p25,
                "market_booked_median": state.market_booked_median,
                "demand": state.demand,
                "listing_occupancy": evidence.get("listing_occupancy"),
                "market_occupancy": state.market_occupancy,
                "market_signal_conflict": evidence.get(
                    "market_signal_conflict"
                ),
                "hard_floor": bands.hard_floor if bands else None,
                "owner_floor": evidence.get("owner_floor"),
                "observed_commission_rate": evidence.get(
                    "observed_commission_rate"
                ),
            }

            self._outcomes.record_execution(
                approval_id=ctx.approval_id,
                run_id=ctx.run_id,
                listing_id=listing_id,
                stay_date=stay_date,
                action=action,
                executed_at=datetime.now(UTC).isoformat(),
                write_outcome=write_outcome,
                cleanup_id=cleanup_id,
                price_before=state.current_price,
                price_after=proposed_price,
                currency=currency,
                days_out=_days_out(stay_date),
                evidence=evidence,
            )

        except Exception:  # noqa: BLE001 - see the docstring
            # Deliberately silent here. Visibility is the health view's job;
            # raising would be the actual harm.
            return

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

        # Same override as the recommendation path, so the fingerprint is
        # computed from the same comp band the recommendation used.
        _band = bands_for(listing_id)

        if _band is not None and _band.bedrooms_override is not None:
            bedrooms = _band.bedrooms_override

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
            events=str(night.get("events")).strip() or None
            if night.get("events")
            else None,
        )

        return state, str(listing.get("currency") or "USD")

    def _confirm(
        self,
        record,
        reason_sent: str,
        result,
    ) -> dict[str, Any] | None:
        """Settle the cleanup row against what the provider actually stored.

        Returns a refusal payload when the write cannot be trusted to be
        cleanable later, or None to let the normal outcome stand.
        """
        if result.outcome is not WriteOutcome.CONFIRMED_APPLIED:
            self._cleanups.resolve(
                record.id,
                CleanupState.NEEDS_REVIEW
                if result.outcome is WriteOutcome.UNKNOWN_WRITE_STATE
                else CleanupState.VANISHED,
                result.message,
            )

            return None

        if result.reason_intact is False:
            # The marker did not survive. Ownership could never be proven, so
            # the override would be uncleanable -- say so now, seconds after
            # the write, rather than a week later when cleanup refuses.
            self._cleanups.resolve(
                record.id,
                CleanupState.NEEDS_REVIEW,
                (
                    "PriceLabs altered the reason it stored, so the ownership "
                    "marker cannot be relied on. The override is in place and "
                    "needs a person to remove it."
                ),
            )

            return {
                "outcome": WriteOutcome.CONFIRMED_APPLIED.value,
                "stay_date": record.stay_date,
                "cleanup_id": record.id,
                "cleanup_state": CleanupState.NEEDS_REVIEW.value,
                "needs_human": True,
                "message": (
                    "The price was applied, but PriceLabs did not store the "
                    "ownership marker intact, so AgentGuard cannot remove this "
                    "override automatically. It needs a person."
                ),
            }

        state = self._cleanups.mark_active(
            record.id,
            result.provider_created_at,
            reason_sent,
            provider_updated_at=result.provider_updated_at,
        )

        if state is CleanupState.NEEDS_REVIEW:
            return {
                "outcome": WriteOutcome.CONFIRMED_APPLIED.value,
                "stay_date": record.stay_date,
                "cleanup_id": record.id,
                "cleanup_state": state.value,
                "needs_human": True,
                "message": (
                    "The price was applied, but the confirming re-read could "
                    "not establish that PriceLabs created this override just "
                    "now, so it cannot be verified as ours later. It needs a "
                    "person."
                ),
            }

        return None

    def apply_pricing_action(
        self,
        listing_id: str,
        stay_date: str,
        action: str,
        fingerprint: str,
        reason: str,
        proposed_price: float | None = None,
        context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Apply one approved action. Never retries, never loops.

        `context` is supplied by `ToolRegistry.execute`, not by the caller.
        The approval and run ids it carries are what tie the cleanup row -- and
        every later audit event about removing this override -- back to the
        human decision that authorised the write. They are deliberately absent
        from `APPLY_PRICING_ACTION_SCHEMA`: an id asserting that a person
        approved something must not be a field anyone can fill in.

        The live proof on 2026-09-05 is why this is a parameter at all. The
        tool previously took `approval_id` and `run_id` as ordinary optional
        arguments, so `tool.function(**arguments)` never passed them and the
        cleanup row recorded `None` for both -- while a unit test that supplied
        them by hand went green.
        """
        ctx = context or ExecutionContext()
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
        blocked = unverified_reason(parsed.value, listing_id)

        if blocked is not None:
            return _refused("UNVERIFIED_BEHAVIOUR", blocked, stay_date)

        # Both kill switches, checked here rather than only at the moment of
        # writing. `PriceLabsWriteClient._guard` still checks them too and must
        # keep doing so -- it is the last line and the only one a future caller
        # cannot route around -- but checking there *alone* made a refusal do
        # real work first: it read the provider four times and left a
        # PENDING_WRITE cleanup row describing an obligation for a write that
        # never happened. A refusal must change nothing, and that includes not
        # creating records and not calling a third party.
        if not writes_enabled():
            return _refused(
                "WRITES_DISABLED",
                "ENABLE_PRICING_WRITES is not enabled; no price was changed.",
                stay_date,
            )

        if not bands.automation_enabled:
            return _refused(
                "WRITES_DISABLED",
                (
                    "Pricing automation is not enabled for this listing; "
                    "no price was changed."
                ),
                stay_date,
            )

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

        record = None

        try:
            if parsed is PriceAction.REMOVE_PIN:
                result = self._writer.remove_override(
                    listing_id,
                    self._pms,
                    stay_date,
                    automation_enabled=bands.automation_enabled,
                )

                self._record_outcome(
                    ctx=ctx,
                    listing_id=listing_id,
                    stay_date=stay_date,
                    action=parsed.value,
                    write_outcome=result.outcome.value,
                    state=state,
                    currency=currency,
                    proposed_price=None,
                    cleanup_id=None,
                )

            else:
                if proposed_price is None:
                    return _refused(
                        "NO_PRICE",
                        "No proposed price was supplied.",
                        stay_date,
                    )

                if self._cleanups is None:
                    # No store means no way to record the obligation, and an
                    # override nobody recorded is the stranded pin this whole
                    # design exists to prevent. Refuse rather than write.
                    return _refused(
                        "NO_CLEANUP_STORE",
                        (
                            "No cleanup store is configured, so this override "
                            "could not be recorded before writing."
                        ),
                        stay_date,
                    )

                # The row comes first. Always.
                record = self._cleanups.record_intent(
                    listing_id=listing_id,
                    pms=self._pms,
                    stay_date=stay_date,
                    old_price=state.pinned_price,
                    new_price=float(proposed_price),
                    currency=currency,
                    cleanup_at=default_cleanup_at(
                        _dt.date.fromisoformat(stay_date)
                    ).isoformat(),
                    approval_id=ctx.approval_id,
                    run_id=ctx.run_id,
                )

                marked = build_reason(record.marker, reason)

                # What we are about to send, recorded before we send it.
                #
                # If the confirming re-read then fails, the row still carries
                # the text AgentGuard composed, so a person investigating an
                # ambiguous write has something to compare the provider's
                # stored reason against. Evidence of intent only: it does not
                # set `provider_created_at`, does not make the row ACTIVE, and
                # grants no cleanup authority whatsoever.
                self._cleanups.record_reason_sent(record.id, marked)

                result = self._writer.set_override(
                    listing_id,
                    self._pms,
                    stay_date,
                    float(proposed_price),
                    currency=currency,
                    reason=marked,
                    automation_enabled=bands.automation_enabled,
                )

                # Before `_confirm`, which has early returns: the price has
                # already moved by now, so the outcome row must exist
                # regardless of which of those paths is taken.
                self._record_outcome(
                    ctx=ctx,
                    listing_id=listing_id,
                    stay_date=stay_date,
                    action=parsed.value,
                    write_outcome=result.outcome.value,
                    state=state,
                    currency=currency,
                    proposed_price=float(proposed_price),
                    cleanup_id=record.id,
                )

                confirmation = self._confirm(record, marked, result)

                if confirmation is not None:
                    return confirmation

        except PricingWritesDisabled as exc:
            return _refused("WRITES_DISABLED", str(exc), stay_date)

        except PriceLabsUnavailable:
            # Ambiguous: the change may already be live. The row is kept with
            # UNKNOWN_WRITE_STATE rather than dropped, so an action that may
            # have happened cannot silently vanish from a denominator.
            self._record_outcome(
                ctx=ctx,
                listing_id=listing_id,
                stay_date=stay_date,
                action=parsed.value,
                write_outcome=WriteOutcome.UNKNOWN_WRITE_STATE.value,
                state=state,
                currency=currency,
                proposed_price=(
                    float(proposed_price) if proposed_price is not None else None
                ),
                cleanup_id=record.id if record is not None else None,
            )

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
    """A refusal is a clean outcome: nothing was sent, nothing changed.

    That is an obligation on every caller, not just a description. It was once
    untrue: a WRITES_DISABLED refusal reached here *after* four provider reads
    and a PENDING_WRITE cleanup row, so the "nothing changed" it reported was
    a claim the code did not keep. Every check that can refuse now runs before
    anything with a side effect. Adding a refusal after a read, a write or a
    record means moving it earlier, not widening what this sentence covers.
    """
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


def _days_out(stay_date: str) -> int | None:
    """Nights between today and arrival, or None if the date cannot be read."""
    try:
        stay = _dt.date.fromisoformat(stay_date)

    except (TypeError, ValueError):
        return None

    return (stay - datetime.now(UTC).date()).days
