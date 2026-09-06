"""Pricing actions: guardrails, staleness, kill switches, and write outcomes.

Every price and date here is invented. No test in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.connectors.pricelabs.pricing_tools import (
    APPLY_PRICING_ACTION_TOOL,
    PriceLabsPricingTools,
)
from app.connectors.pricelabs.write_client import (
    PriceLabsWriteClient,
    PricingWritesDisabled,
    WriteOutcome,
)
from app.pricing_cleanup import CleanupState
from app.pricing_config import MAX_CHANGE_PER_RUN, PricingBands, bands_for
from app.pricing_policy import (
    Confidence,
    MarketState,
    PriceAction,
    Recommendation,
    Refusal,
    check_guardrails,
    clamp_move,
    finalise,
    fingerprint,
)
from app.tool_registry import ExecutionContext

STAY = datetime.date(2026, 9, 20)

BUNKERS = "680444___747423"
HARVARD = "681286___748333"


def bands(**over) -> PricingBands:
    base = {
        "listing_id": "inv-1",
        "slug": "invented",
        "display_name": "Invented Cottage",
        "hard_floor": 140.0,
        "normal_floor": 170.0,
        "auto_raise_ceiling": 260.0,
        "absolute_ceiling": 340.0,
    }

    base.update(over)

    return PricingBands(**base)


def state(**over) -> MarketState:
    base = {
        "current_price": 200.0,
        "market_p25": 240.0,
        "market_booked_median": 300.0,
        "market_occupancy": 50.0,
        "listing_occupancy": 70.0,
        "demand": "Good Demand",
        "pickup_7_days": 9.5,
        "pinned_price": None,
        "last_refreshed_at": "2026-09-04T11:00:00+00:00",
    }

    base.update(over)

    return MarketState(**base)


def rec(action, proposed, *, band=None, conf=Confidence.HIGH, st=None) -> Recommendation:
    st = st or state()

    return finalise(
        Recommendation(
            listing_id=(band or bands()).listing_id,
            slug=(band or bands()).slug,
            display_name="Invented Cottage",
            stay_date=STAY,
            days_out=16,
            action=action,
            current_price=st.current_price,
            proposed_price=proposed,
            confidence=conf,
            reason="test",
            state=st,
            bands=band or bands(),
        )
    )


# -- guardrails ------------------------------------------------------------


def test_lower_within_guardrails_is_actionable():
    r = rec(PriceAction.LOWER, 185.0)

    assert r.action is PriceAction.LOWER
    assert r.is_actionable
    assert r.refused is None


def test_lower_below_the_hard_floor_is_rejected():
    """The one line that must never be crossed."""
    r = rec(PriceAction.LOWER, 135.0, band=bands(hard_floor=140.0))

    assert r.action is PriceAction.HOLD
    assert r.refused is Refusal.EXCEEDS_MAX_CHANGE or r.refused is (
        Refusal.BELOW_HARD_FLOOR
    )
    assert not r.is_actionable


def test_a_lower_that_clears_the_cap_but_breaks_the_floor_is_still_rejected():
    r = rec(
        PriceAction.LOWER,
        139.0,
        band=bands(hard_floor=140.0),
        st=state(current_price=145.0),
    )

    assert r.refused is Refusal.BELOW_HARD_FLOOR
    assert not r.is_actionable


def test_a_lower_between_hard_and_owner_floor_is_flagged_for_a_human():
    r = rec(
        PriceAction.LOWER,
        150.0,
        band=bands(hard_floor=140.0, normal_floor=170.0),
        st=state(current_price=165.0),
    )

    assert r.is_actionable
    assert any("owner floor" in note for note in r.notes)
    assert any("$170" in note for note in r.notes)


def test_raise_within_the_auto_ceiling_is_actionable():
    r = rec(PriceAction.RAISE, 215.0, band=bands(auto_raise_ceiling=260.0))

    assert r.action is PriceAction.RAISE
    assert r.is_actionable


def test_raise_above_the_auto_ceiling_is_rejected():
    r = rec(
        PriceAction.RAISE,
        265.0,
        band=bands(auto_raise_ceiling=260.0),
        st=state(current_price=250.0),
    )

    assert r.action is PriceAction.HOLD
    assert r.refused is Refusal.ABOVE_AUTO_CEILING


def test_raise_above_the_absolute_ceiling_is_rejected_first():
    assert (
        check_guardrails(
            PriceAction.RAISE,
            330.0,
            345.0,
            bands(auto_raise_ceiling=260.0, absolute_ceiling=340.0),
            Confidence.HIGH,
        )
        is Refusal.ABOVE_ABSOLUTE_CEILING
    )


def test_a_move_larger_than_the_cap_is_rejected():
    r = rec(PriceAction.RAISE, 260.0, st=state(current_price=200.0))

    assert r.action is PriceAction.HOLD
    assert r.refused is Refusal.EXCEEDS_MAX_CHANGE


def test_the_cap_is_ten_percent_and_clamping_respects_direction():
    assert MAX_CHANGE_PER_RUN == 0.10
    assert clamp_move(200.0, 300.0) == 220.0
    assert clamp_move(200.0, 100.0) == 180.0


def test_low_confidence_never_becomes_an_action():
    r = rec(PriceAction.LOWER, 190.0, conf=Confidence.LOW)

    assert r.action is PriceAction.HOLD
    assert r.refused is Refusal.LOW_CONFIDENCE


def test_a_listing_without_owner_bands_can_never_be_written_to():
    r = finalise(
        Recommendation(
            listing_id="unknown",
            slug="unknown",
            display_name="Unbanded",
            stay_date=STAY,
            days_out=16,
            action=PriceAction.LOWER,
            current_price=200.0,
            proposed_price=190.0,
            confidence=Confidence.HIGH,
            reason="test",
            state=state(),
            bands=None,
        )
    )

    assert r.refused is Refusal.NO_BANDS
    assert not r.is_actionable


def test_harvard_raise_always_requires_a_human():
    harvard = bands_for(HARVARD)

    assert harvard.raise_requires_human is True

    r = rec(
        PriceAction.RAISE,
        520.0,
        band=harvard,
        st=state(current_price=500.0, market_p25=560.0),
    )

    assert r.is_actionable
    assert r.requires_human is True
    assert any("human decision" in note for note in r.notes)


def test_a_non_harvard_raise_does_not_demand_a_human_by_itself():
    r = rec(PriceAction.RAISE, 215.0)

    assert r.requires_human is False


def test_hold_and_keep_pin_are_never_actionable():
    for action in (PriceAction.HOLD, PriceAction.KEEP_PIN):
        r = rec(action, 200.0)

        assert not r.is_actionable


def test_remove_pin_is_actionable_and_skips_price_bounds():
    r = rec(PriceAction.REMOVE_PIN, None, st=state(pinned_price=109.0))

    assert r.action is PriceAction.REMOVE_PIN
    assert r.is_actionable


# -- fingerprint / staleness ----------------------------------------------


def test_the_fingerprint_is_stable_for_unchanged_state():
    assert fingerprint("a", STAY, state()) == fingerprint("a", STAY, state())


def test_a_price_change_changes_the_fingerprint():
    assert fingerprint("a", STAY, state()) != fingerprint(
        "a", STAY, state(current_price=210.0)
    )


def test_a_pin_change_changes_the_fingerprint():
    assert fingerprint("a", STAY, state(pinned_price=109.0)) != fingerprint(
        "a", STAY, state(pinned_price=119.0)
    )


def test_a_sub_dollar_wobble_does_not_invalidate_a_recommendation():
    """Rounding is deliberate: only a move a person would notice counts."""
    assert fingerprint("a", STAY, state(current_price=200.4)) == fingerprint(
        "a", STAY, state(current_price=200.0)
    )


# -- fakes -----------------------------------------------------------------


def fresh_stamp() -> str:
    return (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    ).isoformat()


class FakeReader:
    """A PriceLabs reader with scripted state. Counts nothing but reads."""

    def __init__(
        self,
        price=200.0,
        override=None,
        refreshed=None,
        demand="Good Demand",
        fail=False,
    ):
        self.price = price
        self.override = override
        self.refreshed = refreshed or fresh_stamp()
        self.demand = demand
        self.fail = fail

    def listings(self):
        if self.fail:
            raise PriceLabsUnavailable("down")

        return [{"id": BUNKERS, "pms": "lodgify", "currency": "USD", "name": "Bunkers"}]

    def listing_prices(self, listings, date_from, date_to):
        if self.fail:
            raise PriceLabsUnavailable("down")

        return [
            {
                "id": BUNKERS,
                "last_refreshed_at": self.refreshed,
                "data": [
                    {
                        "date": date_from,
                        "price": self.price,
                        "demand_desc": self.demand,
                        "booking_status": "",
                        "occupancy": 0.0,
                        "unbookable": 0,
                    }
                ],
            }
        ]

    def overrides(self, listing_id, pms):
        if self.fail:
            raise PriceLabsUnavailable("down")

        return [self.override] if self.override else []

    def neighborhood_data(self, listing_id, pms):
        if self.fail:
            raise PriceLabsUnavailable("down")

        return {
            "Future Percentile Prices": {
                "Labels": [
                    "25th Percentile",
                    "50th Percentile",
                    "75th Percentile",
                    "Median Booked Price",
                    "90th Percentile",
                    "N_Bookings",
                ],
                "Category": {
                    "3": {
                        "X_values": ["2026-09-20"],
                        "Y_values": [[240.0], [300.0], [380.0], [310.0], [460.0], [40]],
                    }
                },
            },
            "Future Occ/New/Canc": {
                "Labels": ["Occupancy"],
                "Category": {"3": {"X_values": ["2026-09-20"], "Y_values": [[[55.0]]]}},
            },
        }


class RecordingWriter:
    """Records every write attempt, so 'exactly once' is checkable."""

    def __init__(self, outcome=None, raises=None):
        self.calls = []
        self.outcome = outcome
        self.raises = raises

    def _result(self, stay_date, old, new):
        from app.connectors.pricelabs.write_client import WriteResult

        if self.raises:
            raise self.raises

        return self.outcome or WriteResult(
            outcome=WriteOutcome.CONFIRMED_APPLIED,
            message="applied",
            stay_date=stay_date,
            old_price=old,
            new_price=new,
        )

    def set_override(self, listing_id, pms, stay_date, price, **kw):
        self.calls.append(("set", listing_id, stay_date, price))

        return self._result(stay_date, None, price)

    def remove_override(self, listing_id, pms, stay_date, **kw):
        self.calls.append(("remove", listing_id, stay_date))

        return self._result(stay_date, 109.0, None)


def store(database):
    from app.pricing_cleanup import PricingCleanupStore

    return PricingCleanupStore(database=database)


def tools(reader, writer, cleanups=None):
    """A price-setting write now needs a cleanup store, by design.

    An override nobody recorded is the stranded pin the whole cleanup design
    exists to prevent, so the tool refuses to write one. Tests that exercise a
    LOWER or RAISE therefore pass a store; `test_a_price_write_without_a_cleanup_
    store_is_refused` covers the absence.
    """
    return PriceLabsPricingTools(
        reader=reader,
        writer=writer,
        pms="lodgify",
        cleanups=cleanups,
    )


def current_fingerprint(reader, stay="2026-09-20"):
    from app.connectors.pricelabs.pricing_tools import fingerprint_of

    t = tools(reader, RecordingWriter())

    st, _ = t._current_state(BUNKERS, stay)

    return fingerprint_of(BUNKERS, stay, st)


def enable(monkeypatch, listing=BUNKERS):
    """Turn both switches on for one listing, exactly as a live run would.

    Also lifts the provider-verification gate, because these tests exercise the
    write machinery rather than the gate itself. Production ships both flags
    False; `test_both_verification_flags_ship_false` is what holds that.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", listing)

    verified(monkeypatch)


def verified(monkeypatch):
    """Treat the provider behaviours as proven, for tests about other things."""
    import app.pricing_config as config

    monkeypatch.setattr(config, "CLEANUP_STRATEGY_VERIFIED", True)
    monkeypatch.setattr(config, "BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED", True)
    monkeypatch.setattr(config, "EXPIRY_SEMANTICS_VERIFIED", True)
    monkeypatch.setattr(config, "DELETE_ENDPOINT_VERIFIED", True)


# -- staleness -------------------------------------------------------------


def test_a_price_change_after_the_recommendation_refuses_execution(monkeypatch, database):
    enable(monkeypatch)  # past the kill switches; these test later checks

    reader = FakeReader(price=200.0)

    stamp = current_fingerprint(reader)

    reader.price = 235.0  # repriced between recommendation and approval

    writer = RecordingWriter()

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "STALE"
    assert writer.calls == [], "nothing may be sent once the state has moved"


def test_a_pin_that_changed_after_the_recommendation_refuses_execution(monkeypatch):
    enable(monkeypatch)  # past the kill switches; these test later checks

    reader = FakeReader(override={"date": "2026-09-20", "price": "109"})

    stamp = current_fingerprint(reader)

    reader.override = {"date": "2026-09-20", "price": "129"}

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="REMOVE_PIN",
        fingerprint=stamp,
        reason="test",
    )

    assert result["refusal"] == "STALE"
    assert writer.calls == []


def test_stale_pricelabs_data_refuses_execution(monkeypatch, database):
    enable(monkeypatch)  # past the kill switches; these test later checks

    old = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=40)
    ).isoformat()

    reader = FakeReader(refreshed=old)

    writer = RecordingWriter()

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint="whatever",
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "STALE_DATA"
    assert writer.calls == []


def test_provider_unavailable_refuses_execution(monkeypatch, database):
    enable(monkeypatch)  # past the kill switches; these test later checks

    reader = FakeReader(fail=True)

    writer = RecordingWriter()

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint="whatever",
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "PROVIDER_UNAVAILABLE"
    assert writer.calls == []


# -- outcomes --------------------------------------------------------------


def test_an_approved_action_sends_exactly_one_write(monkeypatch, database):
    enable(monkeypatch)

    reader = FakeReader(price=200.0)

    stamp = current_fingerprint(reader)

    writer = RecordingWriter()

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="near-term vacancy",
        proposed_price=190.0,
    )

    assert result["outcome"] == WriteOutcome.CONFIRMED_APPLIED.value
    assert len(writer.calls) == 1
    assert writer.calls[0][0] == "set"


def test_remove_pin_sends_a_removal_and_nothing_else(monkeypatch):
    enable(monkeypatch)

    reader = FakeReader(override={"date": "2026-09-20", "price": "109"})

    stamp = current_fingerprint(reader)

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="REMOVE_PIN",
        fingerprint=stamp,
        reason="date is strong",
    )

    assert result["outcome"] == WriteOutcome.CONFIRMED_APPLIED.value
    assert writer.calls == [("remove", BUNKERS, "2026-09-20")]


def test_a_provider_refusal_is_a_clean_confirmed_failure(monkeypatch, database):
    from app.connectors.pricelabs.write_client import WriteResult

    enable(monkeypatch)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    writer = RecordingWriter(
        outcome=WriteResult(
            outcome=WriteOutcome.CONFIRMED_FAILED,
            message="refused",
            stay_date="2026-09-20",
        )
    )

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert result["outcome"] == WriteOutcome.CONFIRMED_FAILED.value
    assert result["needs_human"] is False


def test_an_ambiguous_send_is_unknown_and_is_never_retried(monkeypatch, database):
    enable(monkeypatch)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    writer = RecordingWriter(raises=PriceLabsUnavailable("timeout"))

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert result["outcome"] == WriteOutcome.UNKNOWN_WRITE_STATE.value
    assert result["needs_human"] is True
    assert "do not retry" in result["message"].lower()
    # One attempt was made and nothing repeated it.
    assert len(writer.calls) == 1


# -- kill switches ---------------------------------------------------------


def test_the_global_kill_switch_makes_a_write_impossible(monkeypatch):
    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)

    client = PriceLabsWriteClient(reader=FakeReader(), api_key_provider=lambda: "k")

    with pytest.raises(PricingWritesDisabled, match="ENABLE_PRICING_WRITES"):
        client.set_override(
            BUNKERS, "lodgify", "2026-09-20", 190.0, "USD", "r",
            automation_enabled=True,
        )


def test_the_per_listing_switch_makes_a_write_impossible(monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")

    client = PriceLabsWriteClient(reader=FakeReader(), api_key_provider=lambda: "k")

    with pytest.raises(PricingWritesDisabled, match="not enabled for this listing"):
        client.remove_override(
            BUNKERS, "lodgify", "2026-09-20", automation_enabled=False,
        )


def test_every_listing_ships_with_automation_off():
    from app.pricing_config import BANDS

    assert all(not band.automation_enabled for band in BANDS)


def test_the_kill_switch_is_off_unless_exactly_true(monkeypatch, database):
    from app.pricing_config import writes_enabled

    for value in ("", "false", "1", "yes", "TRUE ", "on"):
        monkeypatch.setenv("ENABLE_PRICING_WRITES", value)

        assert writes_enabled() is (value.strip().lower() == "true")


def test_a_disabled_switch_surfaces_as_a_refusal_not_a_crash(monkeypatch, database):
    verified(monkeypatch)

    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    real = PriceLabsWriteClient(reader=reader, api_key_provider=lambda: "k")

    result = tools(reader, real, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "WRITES_DISABLED"
    assert result["outcome"] == WriteOutcome.CONFIRMED_FAILED.value


# -- the model boundary ----------------------------------------------------


def test_the_model_is_never_told_the_pricing_write_exists(registry):
    from app.tool_setup import apply_pricing_action_tool

    registry.register(apply_pricing_action_tool(tools(FakeReader(), RecordingWriter())))

    advertised = {d.name for d in registry.definitions()}

    assert APPLY_PRICING_ACTION_TOOL not in advertised, (
        "the pricing write must never appear in what the model is told it can do"
    )

    described = {row["name"] for row in registry.describe()}

    assert APPLY_PRICING_ACTION_TOOL in described, (
        "the console must still see the capability"
    )


def test_the_pricing_write_is_dangerous_and_needs_approval(registry):
    from app.tool_registry import ApprovalRequired
    from app.tool_setup import apply_pricing_action_tool

    registry.register(apply_pricing_action_tool(tools(FakeReader(), RecordingWriter())))

    with pytest.raises(ApprovalRequired):
        registry.execute(APPLY_PRICING_ACTION_TOOL, {"listing_id": BUNKERS})


# -- the approval flow -----------------------------------------------------


def install_pricing_tool(api, writer, reader=None, cleanups=None):
    """Register a recording pricing tool on the running app.

    Mirrors production wiring: DANGEROUS and not model-callable, so approval is
    still required and the model still cannot see it.
    """
    from app.tool_setup import apply_pricing_action_tool

    reader = reader or FakeReader()

    tool = apply_pricing_action_tool(
        tools(reader, writer, cleanups or store(api.module.database))
    )

    api.module.tool_registry.register(tool)

    return reader


def submit(api, reader, action="LOWER", price=190.0, role="ADMIN"):
    stamp = current_fingerprint(reader)

    return api.client(role).post(
        "/vacancy/recommendations/submit",
        json={
            "listing_id": BUNKERS,
            "stay_date": "2026-09-20",
            "action": action,
            "proposed_price": price,
            "fingerprint": stamp,
            "reason": "near-term vacancy",
        },
    )


def test_submitting_a_recommendation_writes_nothing_and_parks_for_approval(api):
    writer = RecordingWriter()

    reader = install_pricing_tool(api, writer)

    response = submit(api, reader)

    assert response.status_code == 200, response.text

    body = response.json()

    assert body["approval_required"] is not None
    assert body["approval_required"]["risk"] == "DANGEROUS"
    assert body["status"] == "WAITING_FOR_APPROVAL"
    assert writer.calls == [], "submitting must never change a price"


def test_a_rejected_approval_performs_zero_writes(api, monkeypatch):
    enable(monkeypatch)

    writer = RecordingWriter()

    reader = install_pricing_tool(api, writer)

    approval_id = submit(api, reader).json()["approval_required"]["approval_id"]

    decided = api.client("ADMIN").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": False},
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["approved"] is False
    assert writer.calls == [], "a rejected action must never reach PriceLabs"


def test_an_approved_action_performs_exactly_one_write(api, monkeypatch):
    enable(monkeypatch)

    writer = RecordingWriter()

    reader = install_pricing_tool(api, writer)

    approval_id = submit(api, reader).json()["approval_required"]["approval_id"]

    decided = api.client("ADMIN").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["approved"] is True
    assert len(writer.calls) == 1, "approval must produce exactly one write"
    assert writer.calls[0][0] == "set"


def test_an_informational_action_cannot_be_submitted(api):
    writer = RecordingWriter()

    install_pricing_tool(api, writer)

    for action in ("HOLD", "KEEP_PIN"):
        response = api.client("ADMIN").post(
            "/vacancy/recommendations/submit",
            json={
                "listing_id": BUNKERS,
                "stay_date": "2026-09-20",
                "action": action,
                "fingerprint": "x",
                "reason": "r",
            },
        )

        assert response.status_code == 400
        assert "cannot change a price" in response.json()["detail"]

    assert writer.calls == []


def test_a_listing_without_bands_cannot_be_submitted(api):
    writer = RecordingWriter()

    install_pricing_tool(api, writer)

    response = api.client("ADMIN").post(
        "/vacancy/recommendations/submit",
        json={
            "listing_id": "not-a-listing",
            "stay_date": "2026-09-20",
            "action": "LOWER",
            "proposed_price": 100.0,
            "fingerprint": "x",
            "reason": "r",
        },
    )

    assert response.status_code == 400
    assert writer.calls == []


def test_submitting_requires_permission(api):
    writer = RecordingWriter()

    reader = install_pricing_tool(api, writer)

    response = submit(api, reader, role="VIEWER")

    assert response.status_code == 403
    assert writer.calls == []


def test_a_stale_recommendation_approved_late_still_writes_nothing(api, monkeypatch):
    """The last line of defence: approval does not license a stale change."""
    enable(monkeypatch)

    writer = RecordingWriter()

    reader = install_pricing_tool(api, writer)

    approval_id = submit(api, reader).json()["approval_required"]["approval_id"]

    # The market moves between submission and approval.
    reader.price = 240.0

    decided = api.client("ADMIN").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    assert decided.status_code == 200, decided.text
    assert writer.calls == [], "a stale recommendation must not be executed"


# -- the recommendation/execution seam ------------------------------------


def test_a_freshly_built_recommendation_is_not_refused_as_stale():
    """The regression guard for a bug that made every write impossible.

    The tool must re-read *the same state* the recommendation was built from.
    It once omitted the market reference, so its fingerprint could never match
    the one the service computed -- every action was refused as STALE and the
    write path could not fire at all. Both fingerprints are computed here by
    their own real code paths, never by a shared helper, which is exactly what
    the earlier tests failed to do.
    """
    from app.connectors.pricelabs.pricing_tools import fingerprint_of
    from app.pricing_policy import MarketState
    from app.pricing_recommendations import recommend_night

    reader = FakeReader(price=200.0, demand="Good Demand")

    market = __import__(
        "app.connectors.pricelabs.normalise", fromlist=["parse_market_series"]
    ).parse_market_series(reader.neighborhood_data(BUNKERS, "lodgify"), 3)

    reference = market["2026-09-20"]

    built = recommend_night(
        listing_id=BUNKERS,
        display_name="Bunkers",
        stay_date=datetime.date(2026, 9, 20),
        days_out=16,
        state=MarketState(
            current_price=200.0,
            market_p25=reference["p25"],
            market_booked_median=reference["booked_median"],
            market_occupancy=reference["market_occupancy"],
            listing_occupancy=68.0,
            demand="Good Demand",
            pickup_7_days=None,
            pinned_price=None,
            last_refreshed_at=reader.refreshed,
        ),
        occupancy_gap=32.0,
        history_adr=180.0,
        history_count=5,
        lead_band="15-30d",
        is_pinned=False,
        is_open=True,
    )

    assert built.is_actionable, "expected a RAISE on this state"

    executed, _ = tools(reader, RecordingWriter())._current_state(
        BUNKERS, "2026-09-20"
    )

    assert (
        fingerprint_of(BUNKERS, "2026-09-20", executed) == built.fingerprint
    ), "the tool must re-read the same state the recommendation was built from"


def test_the_execution_state_carries_the_market_reference():
    """Without this the fingerprint silently drops half its inputs."""
    reader = FakeReader()

    state, _ = tools(reader, RecordingWriter())._current_state(BUNKERS, "2026-09-20")

    assert state.market_p25 == 240.0
    assert state.market_booked_median == 310.0
    assert state.market_occupancy == 55.0


def test_a_market_move_after_the_recommendation_refuses_execution(monkeypatch):
    """The protection still works once the market is actually in scope."""
    enable(monkeypatch)  # past the kill switches; these test later checks

    from app.connectors.pricelabs.pricing_tools import fingerprint_of

    reader = FakeReader()

    before, _ = tools(reader, RecordingWriter())._current_state(BUNKERS, "2026-09-20")

    stamp = fingerprint_of(BUNKERS, "2026-09-20", before)

    original = reader.neighborhood_data

    def shifted(listing_id, pms):
        payload = original(listing_id, pms)

        payload["Future Percentile Prices"]["Category"]["3"]["Y_values"][0] = [310.0]

        return payload

    reader.neighborhood_data = shifted

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="RAISE",
        fingerprint=stamp,
        reason="test",
        proposed_price=215.0,
    )

    assert result["refusal"] == "STALE"
    assert writer.calls == []


# -- the per-listing switch ------------------------------------------------


def test_the_per_listing_switch_is_off_for_every_listing_by_default(monkeypatch):
    from app.pricing_config import BANDS

    monkeypatch.delenv("PRICELABS_AUTOMATION_ENABLED", raising=False)

    for band in BANDS:
        assert bands_for(band.listing_id).automation_enabled is False


def test_enabling_one_listing_leaves_every_other_listing_off(monkeypatch):
    from app.pricing_config import BANDS

    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "modern-condo")

    enabled = [
        band.slug for band in BANDS if bands_for(band.listing_id).automation_enabled
    ]

    assert enabled == ["modern-condo"]


def test_the_switch_accepts_a_listing_id_as_well_as_a_slug(monkeypatch):
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "680447___747426")

    assert bands_for("680447___747426").automation_enabled is True
    assert bands_for(BUNKERS).automation_enabled is False


def test_the_console_reports_the_real_switch_state_not_the_table(monkeypatch):
    """A console that says a safety control is off while it is on misinforms."""
    from app.pricing_service import bands_payload

    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "modern-condo")

    rows = {row["slug"]: row["automation_enabled"] for row in bands_payload()}

    assert rows["modern-condo"] is True
    assert all(
        enabled is False for slug, enabled in rows.items() if slug != "modern-condo"
    )


# -- unverified provider behaviour ----------------------------------------


def test_a_lower_is_blocked_when_neither_verified_nor_authorized(
    monkeypatch, database
):
    """Two independent ways past the channel gate, and neither is present here.

    `BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED` would mean the exposure was
    measured. `OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE` means
    it was not and the owner accepted the risk anyway. With both off, a
    published price minus an unknown discount is an unknown guest-facing rate
    that cannot be shown to respect any floor.
    """
    import app.pricing_config as config

    monkeypatch.setattr(
        config, "OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE", False
    )
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    writer = RecordingWriter()

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "UNVERIFIED_BEHAVIOUR"
    assert "Booking.com" in result["message"]
    assert writer.calls == [], "no write may reach PriceLabs while this is open"

    assert writer.calls == [], "no write may reach PriceLabs while this is open"


def test_lowering_is_blocked_separately_by_channel_discount_exposure(
    monkeypatch, database
):
    """Two independent gates stand in front of a LOWER.

    Proving the cleanup lifecycle does not answer what a guest actually pays
    after Booking.com discounts it, so it cannot unblock a price cut on a
    property that sells there.
    """
    import app.pricing_config as config

    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)
    monkeypatch.setattr(config, "CLEANUP_STRATEGY_VERIFIED", True)
    monkeypatch.setattr(config, "BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED", False)
    monkeypatch.setattr(
        config, "OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE", False
    )

    reader = FakeReader()

    writer = RecordingWriter()

    result = tools(reader, writer, store(database)).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=current_fingerprint(reader),
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "UNVERIFIED_BEHAVIOUR"
    assert "Booking.com" in result["message"]
    assert writer.calls == []

    # A RAISE is not blocked by it: a raise cannot lower a published price, so
    # an unknown discount cannot carry it below a floor.
    assert config.unverified_reason("RAISE", BUNKERS) is None


def test_remove_pin_is_unblocked_now_that_delete_is_verified(monkeypatch):
    """DELETE was live-verified on 2026-09-13, so REMOVE_PIN may execute.

    The unlock is deliberately narrow: it says nothing about the fixed-price
    lifecycle, which `test_a_fixed_price_write_is_blocked_while_expiry_is_
    unverified` still holds shut.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    reader = FakeReader(override={"date": "2026-09-20", "price": "109"})

    stamp = current_fingerprint(reader)

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="REMOVE_PIN",
        fingerprint=stamp,
        reason="test",
    )

    assert result.get("refusal") is None
    assert result["outcome"] == WriteOutcome.CONFIRMED_APPLIED.value
    assert writer.calls == [("remove", BUNKERS, "2026-09-20")]


def test_the_gate_is_checked_before_anything_is_read(monkeypatch):
    """A blocked action must not even touch the provider."""
    import app.pricing_config as config

    monkeypatch.setattr(
        config, "OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE", False
    )
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    reader = FakeReader(fail=True)  # any read would raise

    # LOWER is the action still gated, so it is the one that proves the
    # ordering: a blocked action is refused before the provider is touched.
    result = tools(reader, RecordingWriter()).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint="x",
        reason="test",
        proposed_price=140.0,
    )

    assert result["refusal"] == "UNVERIFIED_BEHAVIOUR"


def test_only_the_verified_behaviour_is_unlocked():
    """Each flag reflects exactly what has been proven against the provider.

    DELETE live-verified 2026-09-04, so REMOVE_PIN is open. The explicit
    cleanup lifecycle live-verified 2026-09-05, so RAISE is open. Neither
    result says anything about the Booking.com discount exposure, so LOWER
    stays shut -- and neither says anything about provider-side expiry, which
    is why EXPIRY_SEMANTICS_VERIFIED is still False and is informational
    rather than a permission.
    """
    from app.pricing_config import (
        BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED,
        CLEANUP_STRATEGY_VERIFIED,
        DELETE_ENDPOINT_VERIFIED,
        EXPIRY_SEMANTICS_VERIFIED,
        unverified_reason,
    )

    assert DELETE_ENDPOINT_VERIFIED is True
    assert CLEANUP_STRATEGY_VERIFIED is True
    assert BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED is False
    assert EXPIRY_SEMANTICS_VERIFIED is False

    assert unverified_reason("REMOVE_PIN", BUNKERS) is None
    assert unverified_reason("RAISE", BUNKERS) is None

    # LOWER is open, but not because anything was verified: the owner
    # authorized acting under the known uncertainty. The verification flag
    # above is still False and still means what it says.
    from app.pricing_config import (
        OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE as AUTHORIZED,
    )

    assert AUTHORIZED is True
    assert unverified_reason("LOWER", BUNKERS) is None


def test_the_block_is_surfaced_on_the_recommendation(monkeypatch):
    """The console must say so before a person spends a decision on it."""
    import app.pricing_config as config

    monkeypatch.setattr(
        config, "OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE", False
    )

    from app.pricing_policy import to_payload

    payload = to_payload(
        rec(PriceAction.LOWER, 185.0, band=bands(listing_id=BUNKERS))
    )

    assert payload["actionable"] is True
    assert payload["blocked_reason"] is not None
    assert "Booking.com" in payload["blocked_reason"]


def test_an_authorized_lower_carries_the_warning_instead_of_a_block():
    """Authorized is not silent. The uncertainty travels with the card."""
    from app.pricing_config import BOOKING_COM_UNCERTAINTY_WARNING
    from app.pricing_policy import to_payload

    payload = to_payload(
        rec(PriceAction.LOWER, 185.0, band=bands(listing_id=BUNKERS))
    )

    assert payload["blocked_reason"] is None
    assert payload["booking_com_warning"] == BOOKING_COM_UNCERTAINTY_WARNING


def test_an_informational_recommendation_carries_no_block():
    payload = __import__(
        "app.pricing_policy", fromlist=["to_payload"]
    ).to_payload(rec(PriceAction.HOLD, 200.0))

    assert payload["blocked_reason"] is None


def test_applied_never_claims_the_channel_price_changed():
    """CONFIRMED_APPLIED means stored, and must not read as anything more."""
    from app.connectors.pricelabs.write_client import WriteOutcome

    doc = " ".join((WriteOutcome.__doc__ or "").split())

    assert "does *not* mean the channel price changed" in doc


def test_removing_a_pin_is_described_as_override_cleanup_not_a_booking_change():
    """Wording matters where it will be read by someone deciding what happened.

    Removing an override on a booked night touches the published price and
    nothing else. Describing it as a change to a reservation or a guest's rate
    would misrepresent it in exactly the record a person would rely on.
    """
    from app.connectors.pricelabs.write_client import PriceLabsWriteClient

    doc = " ".join((PriceLabsWriteClient.remove_override.__doc__ or "").split())

    assert "does not alter a reservation" in doc
    assert "housekeeping" in doc
    # The docstring must say the wording holds for both kinds of date, not
    # assume every override belongs to a sellable one.
    assert "housekeeping on a booked date" in doc


def test_the_removal_message_disclaims_touching_a_reservation(monkeypatch):
    enable(monkeypatch)

    reader = FakeReader(override={"date": "2026-09-20", "price": "109"})

    client = PriceLabsWriteClient(reader=reader, api_key_provider=lambda: "k")

    sent = {}

    def fake_send(method, listing_id, body):
        sent["method"] = method
        reader.override = None  # the provider really removed it
        return True

    monkeypatch.setattr(client, "_send", fake_send)

    result = client.remove_override(
        BUNKERS, "lodgify", "2026-09-20", automation_enabled=True
    )

    assert sent["method"] == "DELETE"
    assert result.outcome is WriteOutcome.CONFIRMED_APPLIED
    assert "Pricing override cleaned up" in result.message
    assert "No reservation, guest rate or availability was touched" in result.message

    # The message has to hold for a booked date as well as a sellable one. The
    # first live DELETE was deliberately run against a *booked* night, so
    # calling every removed override a night "still for sale" would contradict
    # the very verification it reports. The sellable case is stated
    # conditionally instead.
    assert "still for sale" not in result.message
    assert "If the date is sellable" in result.message


# -- a completed action must stop being recommended ------------------------


def pinned_night(is_pinned: bool):
    """One strong, still-open night, with the pin present or already gone."""
    from app.pricing_recommendations import recommend_night

    return recommend_night(
        listing_id=BUNKERS,
        display_name="Bunkers",
        stay_date=datetime.date(2026, 9, 20),
        days_out=6,
        state=MarketState(
            current_price=179.0,
            market_p25=250.0,
            market_booked_median=320.0,
            market_occupancy=60.0,
            listing_occupancy=68.0,
            demand="Good Demand",
            pickup_7_days=10.0,
            pinned_price=179.0 if is_pinned else None,
            last_refreshed_at=fresh_stamp(),
        ),
        occupancy_gap=32.0,
        history_adr=200.0,
        history_count=6,
        lead_band="4-7d",
        is_pinned=is_pinned,
        is_open=True,
    )


def test_a_pinned_night_is_recommended_for_removal():
    assert pinned_night(True).action is PriceAction.REMOVE_PIN


def test_once_the_pin_is_gone_the_card_is_gone():
    """A completed action must stop being offered on the next load.

    The recommendation is derived from live override state rather than from
    anything remembered, so a removed pin cannot keep producing a card an owner
    has already acted on. Verified live on 2026-09-05: six approved removals
    left zero REMOVE_PIN cards on the next refresh of /vacancy.
    """
    after = pinned_night(False)

    assert after.action is not PriceAction.REMOVE_PIN
    assert not after.is_actionable or after.action is not PriceAction.REMOVE_PIN


def test_a_price_write_without_a_cleanup_store_is_refused(monkeypatch):
    """No store means no way to record the obligation, so no write happens.

    An override nobody recorded is exactly the stranded pin the cleanup design
    exists to prevent, so the absence of a store is a refusal rather than a
    write that quietly skips its bookkeeping.
    """
    enable(monkeypatch)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    writer = RecordingWriter()

    result = tools(reader, writer, cleanups=None).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "NO_CLEANUP_STORE"
    assert writer.calls == []


def test_a_price_write_records_its_cleanup_row_before_sending(monkeypatch, database):
    """The row must exist first, not be written after a successful send."""
    enable(monkeypatch)

    cleanups = store(database)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    seen: list[int] = []

    class Watching(RecordingWriter):
        def set_override(self, *args, **kwargs):
            # How many rows exist at the moment the write is attempted.
            seen.append(len(cleanups.open_records()))

            return super().set_override(*args, **kwargs)

    tools(reader, Watching(), cleanups).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert seen == [1], "the cleanup row must already exist when the write goes out"


# -- the cleanup row a price write must leave behind ----------------------


def confirming_writer(created="2026-09-05T09:00:00.000Z", updated=None, intact=True):
    """A writer whose confirming re-read behaves like a real PriceLabs create."""
    from app.connectors.pricelabs.write_client import WriteResult

    class Confirming(RecordingWriter):
        def set_override(self, listing_id, pms, stay_date, price, **kw):
            self.calls.append(("set", listing_id, stay_date, price))
            self.reason = kw.get("reason")

            return WriteResult(
                outcome=WriteOutcome.CONFIRMED_APPLIED,
                message="applied",
                stay_date=stay_date,
                old_price=None,
                new_price=price,
                provider_created_at=created,
                provider_updated_at=created if updated is None else updated,
                reason_intact=intact,
            )

    return Confirming()


def write_once(monkeypatch, database, writer, action="LOWER", price=190.0):
    enable(monkeypatch)

    cleanups = store(database)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    result = tools(reader, writer, cleanups).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action=action,
        fingerprint=stamp,
        reason="near-term vacancy",
        proposed_price=price,
        context=ExecutionContext(run_id="run-77", approval_id="ap-77"),
    )

    return result, cleanups


def test_the_reason_sent_carries_this_row_s_own_marker(monkeypatch, database):
    """The token in the provider's `reason` is the record's uuid, exactly.

    Ownership a week later rests entirely on this equality: a marker that
    belonged to some other row, or was reformatted, would leave the override
    unprovable and therefore unremovable.
    """
    from app.pricing_cleanup import build_reason, marker_of

    writer = confirming_writer()

    _, cleanups = write_once(monkeypatch, database, writer)

    rows = cleanups.open_records()

    assert len(rows) == 1

    record = rows[0]

    assert marker_of(writer.reason) == record.marker == record.id
    assert writer.reason == build_reason(record.marker, "near-term vacancy")
    assert record.state == CleanupState.ACTIVE.value


def test_a_reason_the_provider_altered_needs_review(monkeypatch, database):
    """The marker did not survive, so the override could never be proven ours.

    Caught seconds after the write rather than a week later when cleanup
    refuses -- and the row is parked, so no automatic removal is ever tried.
    """
    result, cleanups = write_once(
        monkeypatch,
        database,
        confirming_writer(intact=False),
    )

    record = cleanups.open_records()[0]

    assert record.state == CleanupState.NEEDS_REVIEW.value
    assert result["needs_human"] is True
    assert "needs a person" in result["message"]

    # Far enough ahead that an ACTIVE row would certainly be due. A parked one
    # never is, which is what stops cleanup ever touching this override.
    assert cleanups.due(now=datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC)) == []


def test_a_write_with_no_provider_creation_time_needs_review(monkeypatch, database):
    result, cleanups = write_once(
        monkeypatch,
        database,
        confirming_writer(created=None),
    )

    assert cleanups.open_records()[0].state == CleanupState.NEEDS_REVIEW.value
    assert result["needs_human"] is True


def test_a_write_onto_an_existing_override_needs_review(monkeypatch, database):
    """`updated_at != created_at`: modified, not created."""
    result, cleanups = write_once(
        monkeypatch,
        database,
        confirming_writer(updated="2026-09-05T09:07:00.000Z"),
    )

    assert cleanups.open_records()[0].state == CleanupState.NEEDS_REVIEW.value
    assert result["needs_human"] is True


def test_the_cleanup_row_records_the_approval_that_authorised_the_write(
    monkeypatch,
    database,
):
    """Unit-level: given a context, the row records it.

    Necessary but **not sufficient**, and the live proof on 2026-09-05 is why:
    this passed while production wrote `None` for both ids, because nothing
    was supplying the context. `test_the_production_path_records_the_real_
    approval_and_run` is what proves the runtime actually does.
    """
    _, cleanups = write_once(monkeypatch, database, confirming_writer())

    record = cleanups.open_records()[0]

    assert record.approval_id == "ap-77"
    assert record.run_id == "run-77"
    assert record.stay_date == "2026-09-20"
    assert record.new_price == 190.0


# -- the operator route ---------------------------------------------------


class RecordingRunner:
    """Stands in for the real runner, so the route's own behaviour is visible."""

    def __init__(self, outcomes=None, raises=None):
        self.calls = []
        self.outcomes = outcomes or []
        self.raises = raises

    def run_once(self, now=None):
        self.calls.append(now)

        if self.raises:
            raise self.raises

        return self.outcomes


def install_runner(api, runner):
    api.module.pricelabs_cleanup_runner = runner

    return runner


def test_the_cleanup_route_drives_the_runner_and_nothing_else(api):
    """One implementation. The route is a handle on it, not a second copy."""
    runner = install_runner(api, RecordingRunner())

    response = api.client("ADMIN").post("/pricing/cleanup/run")

    assert response.status_code == 200
    assert len(runner.calls) == 1
    assert response.json() == {
        "processed": 0,
        "deleted": 0,
        "by_state": {},
        "ran_at": response.json()["ran_at"],
        "records": [],
    }


def test_the_cleanup_route_cannot_be_aimed_at_a_date(api):
    """No listing, no date, no record id -- the route takes no input at all.

    A body naming a night is ignored entirely, because there is no parameter
    for it to bind to. The only work a pass can do is what the store already
    holds as owed.
    """
    runner = install_runner(api, RecordingRunner())

    response = api.client("ADMIN").post(
        "/pricing/cleanup/run",
        json={
            "listing_id": BUNKERS,
            "stay_date": "2026-12-25",
            "record_id": "anything",
        },
    )

    assert response.status_code == 200
    assert runner.calls == [None], "the route passes no target of any kind"


def test_the_cleanup_route_requires_administer(api):
    runner = install_runner(api, RecordingRunner())

    for role in ("VIEWER", "OPERATOR", "APPROVER"):
        assert api.client(role).post("/pricing/cleanup/run").status_code == 403

    assert runner.calls == [], "no pass may run for a role that lacks the permission"


def test_the_cleanup_route_reports_what_the_pass_did(api):
    from app.pricing_cleanup import CleanupState
    from app.pricing_cleanup_runner import CleanupOutcome

    install_runner(
        api,
        RecordingRunner(
            outcomes=[
                CleanupOutcome(
                    record_id="c-1",
                    listing_id=BUNKERS,
                    stay_date="2026-09-20",
                    state=CleanupState.CLEANED_UP,
                    detail="removed",
                    deleted=True,
                )
            ]
        ),
    )

    body = api.client("ADMIN").post("/pricing/cleanup/run").json()

    assert body["processed"] == 1
    assert body["deleted"] == 1
    assert body["by_state"] == {"CLEANED_UP": 1}
    assert body["records"][0]["id"] == "c-1"


def test_the_cleanup_route_is_unavailable_without_a_connector(api):
    api.module.pricelabs_cleanup_runner = None

    assert api.client("ADMIN").post("/pricing/cleanup/run").status_code == 503


def test_a_provider_outage_surfaces_as_a_gateway_error_not_a_trace(api):
    install_runner(api, RecordingRunner(raises=PriceLabsUnavailable("down")))

    response = api.client("ADMIN").post("/pricing/cleanup/run")

    assert response.status_code == 502
    assert "down" not in response.json()["detail"]


def test_the_hourly_job_drives_the_same_runner_as_the_route(api, capsys):
    """The job is a scheduler's handle on the one runner, not a second path."""
    from app.pricing_cleanup_job import main as run_job

    runner = install_runner(api, RecordingRunner())

    assert run_job() == 0
    assert len(runner.calls) == 1

    import json as _json

    assert _json.loads(capsys.readouterr().out)["processed"] == 0


def test_the_hourly_job_reports_an_outage_without_pretending_it_ran(api, capsys):
    install_runner(api, RecordingRunner(raises=PriceLabsUnavailable("down")))

    from app.pricing_cleanup_job import main as run_job

    assert run_job() == 1
    assert capsys.readouterr().out == ""


# -- execution context, through the path production actually uses ---------


def test_the_production_path_records_the_real_approval_and_run(api, monkeypatch):
    """The whole chain, end to end, with nothing hand-fed.

    request_action -> approval created -> approval granted -> ToolRegistry
    .execute -> apply_pricing_action -> record_intent, then the cleanup runner
    and its audit event. No test supplies an approval id anywhere; the ids
    asserted below are the ones the runtime minted.

    This exists because the unit-level version passed while production wrote
    `None` for both. A test that hands a function the value it is meant to
    obtain from elsewhere proves the parameter works, not that anything uses
    it -- and the live proof on 2026-09-05 is where that showed up, on a real
    override against a real listing.
    """
    enable(monkeypatch)

    cleanups = store(api.module.database)

    writer = confirming_writer()

    reader = install_pricing_tool(api, writer, cleanups=cleanups)

    submitted = submit(api, reader, action="LOWER", price=190.0).json()

    run_id = submitted["run_id"]
    approval_id = submitted["approval_required"]["approval_id"]

    assert run_id and approval_id

    # The stored arguments are the business ones only. Neither id is in them.
    from app.db_models import ApprovalRecord

    with api.module.database.session() as session:
        stored = session.get(ApprovalRecord, approval_id).arguments

    assert set(stored) == {
        "listing_id",
        "stay_date",
        "action",
        "proposed_price",
        "fingerprint",
        "reason",
    }

    resolved = api.client("ADMIN").post(
        f"/agent/approvals/{approval_id}",
        json={"approved": True},
    )

    assert resolved.status_code == 200

    rows = cleanups.open_records()

    assert len(rows) == 1

    record = rows[0]

    assert record.state == CleanupState.ACTIVE.value
    assert record.approval_id == approval_id
    assert record.run_id == run_id

    # ...and the cleanup that eventually removes the override carries them too.
    from app.pricing_cleanup_runner import PricingCleanupRunner

    class Reader:
        def overrides(self, listing_id, pms):
            return [
                {
                    "date": record.stay_date,
                    "price": str(round(record.new_price)),
                    "reason": record.reason_sent,
                    "created_at": record.provider_created_at,
                    "updated_at": record.provider_created_at,
                }
            ]

    import datetime as _dt

    cleanups._update(
        record.id,
        cleanup_at=(
            _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1)
        ).isoformat(),
    )

    PricingCleanupRunner(
        cleanups,
        Reader(),
        RemovingWriter(),
        audit=api.module.audit_store,
    ).run_once()

    events = [
        e
        for e in api.module.audit_store.list_events(limit=200)
        if e["event_type"] == "PRICING_CLEANUP"
    ]

    assert len(events) == 1
    assert events[0]["details"]["approval_id"] == approval_id
    assert events[0]["run_id"] == run_id


class RemovingWriter(RecordingWriter):
    def remove_override(self, listing_id, pms, stay_date, **kw):
        from app.connectors.pricelabs.write_client import WriteResult

        self.calls.append(("remove", listing_id, stay_date))

        return WriteResult(
            outcome=WriteOutcome.CONFIRMED_APPLIED,
            message="removed",
            stay_date=stay_date,
        )


def test_the_model_is_never_told_the_context_exists():
    """Neither id is a schema property, so nothing can propose one."""
    from app.connectors.pricelabs.pricing_tools import APPLY_PRICING_ACTION_SCHEMA

    properties = APPLY_PRICING_ACTION_SCHEMA["properties"]

    assert "approval_id" not in properties
    assert "run_id" not in properties
    assert "context" not in properties
    assert APPLY_PRICING_ACTION_SCHEMA["additionalProperties"] is False


def test_a_caller_cannot_occupy_the_context_slot():
    """`context` in the arguments is refused, not merged and not ignored.

    Ignoring it would be safe today and a trap later: a future tool reading
    `arguments["context"]` would silently trust a caller-supplied value.
    """
    from app.tool_registry import ExecutionContext, Tool, ToolRegistry, ToolRisk

    seen = {}

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="ctx",
            description="d",
            function=lambda context=None: seen.setdefault("ctx", context),
            parameters={"type": "object", "properties": {}},
            risk=ToolRisk.READ,
            wants_context=True,
        )
    )

    with pytest.raises(ValueError, match="execution context"):
        registry.execute(
            "ctx",
            {"context": ExecutionContext(approval_id="forged")},
        )

    assert seen == {}, "nothing may run once a caller has tried to forge context"


def test_a_tool_that_did_not_opt_in_is_never_handed_context():
    from app.tool_registry import ExecutionContext, Tool, ToolRegistry, ToolRisk

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="plain",
            description="d",
            function=lambda: "ok",
            parameters={"type": "object", "properties": {}},
            risk=ToolRisk.READ,
        )
    )

    # Would raise TypeError if the context were passed to a tool taking none.
    assert registry.execute("plain", {}, context=ExecutionContext(run_id="r")) == "ok"


def test_one_approvals_context_cannot_leak_into_another_execution(api, monkeypatch):
    """Two approvals, two rows, each naming its own decision."""
    enable(monkeypatch)

    cleanups = store(api.module.database)

    reader = install_pricing_tool(api, confirming_writer(), cleanups=cleanups)

    first = submit(api, reader, action="LOWER", price=190.0).json()

    api.client("ADMIN").post(
        f"/agent/approvals/{first['approval_required']['approval_id']}",
        json={"approved": True},
    )

    second = submit(api, reader, action="LOWER", price=191.0).json()

    api.client("ADMIN").post(
        f"/agent/approvals/{second['approval_required']['approval_id']}",
        json={"approved": True},
    )

    rows = {r.approval_id: r.run_id for r in cleanups.open_records()}

    expected = {
        first["approval_required"]["approval_id"]: first["run_id"],
        second["approval_required"]["approval_id"]: second["run_id"],
    }

    assert rows == expected
    assert len(rows) == 2, "each execution recorded its own approval, not a shared one"


# -- what CLEANUP_STRATEGY_VERIFIED=True actually means --------------------
#
# The flag records that the explicit-cleanup lifecycle was proven live on
# 2026-09-05. It is a *verification* record, not a permission, and these hold
# it to that. Opening it released RAISE from one gate and from nothing else:
# two runtime switches and an individual human approval still stand between a
# recommendation and a real listing.


class ExplodingProvider:
    """Fails the test on any provider contact at all, read or write."""

    def __init__(self):
        self.touched = []

    def _forbid(self, what):
        self.touched.append(what)

        raise AssertionError(f"the provider was contacted: {what}")

    def listings(self):
        self._forbid("listings")

    def listing_prices(self, *a, **kw):
        self._forbid("listing_prices")

    def overrides(self, *a, **kw):
        self._forbid("overrides")

    def neighborhood_data(self, *a, **kw):
        self._forbid("neighborhood_data")

    def set_override(self, *a, **kw):
        self._forbid("set_override")

    def remove_override(self, *a, **kw):
        self._forbid("remove_override")


def real_write_client(reader):
    """The production write client, with a fake reader and no network.

    Deliberately not `RecordingWriter`: the kill switches live in
    `PriceLabsWriteClient._guard`, so a test double that does not implement
    them would sail past the very control it claims to be testing. `_guard`
    runs before any request is built, so nothing here can reach the network.
    """
    from app.connectors.pricelabs.write_client import PriceLabsWriteClient

    return PriceLabsWriteClient(
        reader=reader,
        api_key_provider=lambda: (_ for _ in ()).throw(
            AssertionError("a credential was resolved, so a request was built")
        ),
    )


def test_the_verified_cleanup_gate_does_not_by_itself_permit_a_raise(
    monkeypatch,
    database,
):
    """Production defaults: the gate is open and no price can still move.

    This is the whole meaning of the flag. `CLEANUP_STRATEGY_VERIFIED = True`
    says one provider behaviour was proven; it does not say a price may move.

    With `ENABLE_PRICING_WRITES` unset and an empty allowlist -- the shipped
    defaults -- a perfectly valid RAISE is refused **before any provider read
    or write, before the credential is resolved, and before a cleanup row is
    created**. A disabled action does nothing at all.
    """
    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)
    monkeypatch.delenv("PRICELABS_AUTOMATION_ENABLED", raising=False)

    import app.pricing_config as config

    assert config.CLEANUP_STRATEGY_VERIFIED is True
    assert config.unverified_reason("RAISE", BUNKERS) is None, (
        "the cleanup gate is open, so any refusal below is a different control"
    )
    assert config.writes_enabled() is False
    assert not config.automation_allowlist()

    provider = ExplodingProvider()  # any provider contact fails the test

    result = tools(
        provider,
        real_write_client(provider),
        store(database),
    ).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="RAISE",
        fingerprint="whatever",
        reason="test",
        proposed_price=215.0,
    )

    assert provider.touched == [], "no read and no write may reach PriceLabs"
    assert result["outcome"] == WriteOutcome.CONFIRMED_FAILED.value
    assert result["refusal"] == "WRITES_DISABLED"
    assert "ENABLE_PRICING_WRITES" in result["message"]

    assert store(database).open_records() == [], (
        "a refusal must leave no record of an obligation that never existed"
    )


def test_a_disabled_raise_is_refused_by_the_switch_and_not_by_the_gate(
    monkeypatch,
    database,
):
    """Name the control that actually stopped it.

    A refusal reading UNVERIFIED_BEHAVIOUR here would mean the cleanup gate
    was still shut and this flag had not taken effect; WRITES_DISABLED means
    the gate opened and the kill switch is what holds. Distinguishing them is
    the difference between two safety controls that look identical from
    outside.
    """
    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)
    monkeypatch.delenv("PRICELABS_AUTOMATION_ENABLED", raising=False)

    provider = ExplodingProvider()

    result = tools(
        provider,
        real_write_client(provider),
        store(database),
    ).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="RAISE",
        fingerprint="whatever",
        reason="test",
        proposed_price=215.0,
    )

    assert result["refusal"] == "WRITES_DISABLED"
    assert provider.touched == []


def test_the_global_switch_alone_is_not_enough_for_a_raise(monkeypatch, database):
    """Two switches, independent. The listing allowlist is the second."""
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "")

    provider = ExplodingProvider()

    result = tools(
        provider,
        real_write_client(provider),
        store(database),
    ).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="RAISE",
        fingerprint="whatever",
        reason="test",
        proposed_price=215.0,
    )

    assert result["refusal"] == "WRITES_DISABLED"
    assert "not enabled for this listing" in result["message"]
    assert provider.touched == [], "an un-allowlisted listing is never read"
    assert store(database).open_records() == []


def test_remove_pin_with_the_switches_off_is_refused_before_any_provider_access(
    monkeypatch,
    database,
):
    """The early check covers every action that can write, not just prices."""
    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)
    monkeypatch.delenv("PRICELABS_AUTOMATION_ENABLED", raising=False)

    import app.pricing_config as config

    assert config.unverified_reason("REMOVE_PIN", BUNKERS) is None, (
        "REMOVE_PIN is past its verification gate, so the switch is what holds"
    )

    provider = ExplodingProvider()

    result = tools(
        provider,
        real_write_client(provider),
        store(database),
    ).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="REMOVE_PIN",
        fingerprint="whatever",
        reason="test",
    )

    assert result["refusal"] == "WRITES_DISABLED"
    assert provider.touched == []
    assert store(database).open_records() == []


def test_the_write_client_still_guards_even_when_reached_directly(monkeypatch):
    """Defence in depth: the early check did not replace `_guard`.

    A future caller that bypassed `apply_pricing_action` -- or a reordering
    that dropped the early check -- must still hit a closed door at the client.
    """
    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)

    from app.connectors.pricelabs.write_client import PricingWritesDisabled

    client = real_write_client(FakeReader())

    for call in (
        lambda: client.set_override(
            BUNKERS, "lodgify", "2026-09-20", 215.0,
            currency="USD", reason="r", automation_enabled=True,
        ),
        lambda: client.remove_override(
            BUNKERS, "lodgify", "2026-09-20", automation_enabled=True,
        ),
    ):
        with pytest.raises(PricingWritesDisabled):
            call()

    # ...and with the global switch on but the listing off.
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")

    with pytest.raises(PricingWritesDisabled):
        client.remove_override(
            BUNKERS, "lodgify", "2026-09-20", automation_enabled=False,
        )


def test_a_raise_still_needs_an_approval_even_with_the_switches_on(
    api,
    monkeypatch,
):
    """Switches on is not the same as executable.

    `apply_pricing_action` is DANGEROUS, so `ToolRegistry.execute` raises
    `ApprovalRequired` and the run parks. Submitting is not executing: the
    write happens only after a person resolves that specific approval.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    import app.pricing_config as config

    assert config.CLEANUP_STRATEGY_VERIFIED is True
    assert config.writes_enabled() is True

    writer = RecordingWriter()

    reader = install_pricing_tool(api, writer)

    response = submit(api, reader, action="RAISE", price=215.0)

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "WAITING_FOR_APPROVAL"
    assert body["approval_required"]["risk"] == "DANGEROUS"
    assert body["approval_required"]["arguments"]["action"] == "RAISE"
    assert writer.calls == [], "submitting must not execute"

    tool = api.module.tool_registry.get(APPLY_PRICING_ACTION_TOOL)

    assert tool.model_callable is False, "the model is never told this exists"


def test_lower_is_blocked_for_every_property_before_any_provider_access(
    monkeypatch,
    database,
):
    """All seven, by the channel gate, and refused before a single read.

    Held per listing rather than spot-checked: the Booking.com exposure is a
    per-property fact, and a future listing that did not sell there would be a
    deliberate change to this list rather than a silent one.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "")

    import app.pricing_config as config

    monkeypatch.setattr(
        config, "OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE", False
    )

    assert config.BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED is False
    assert len(config.BANDS) == 7

    for band in config.BANDS:
        provider = ExplodingProvider()

        result = tools(provider, provider, store(database)).apply_pricing_action(
            listing_id=band.listing_id,
            stay_date="2026-09-20",
            action="LOWER",
            fingerprint="whatever",
            reason="test",
            proposed_price=band.hard_floor + 1,
        )

        assert provider.touched == [], f"{band.slug}: provider was contacted"
        assert result["refusal"] == "UNVERIFIED_BEHAVIOUR", band.slug
        assert "Booking.com" in result["message"], band.slug
