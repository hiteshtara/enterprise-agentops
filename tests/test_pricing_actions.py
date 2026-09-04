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


def test_a_lower_between_hard_and_normal_floor_is_flagged_for_a_human():
    r = rec(
        PriceAction.LOWER,
        150.0,
        band=bands(hard_floor=140.0, normal_floor=170.0),
        st=state(current_price=165.0),
    )

    assert r.is_actionable
    assert any("normal floor" in note for note in r.notes)


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


def tools(reader, writer):
    return PriceLabsPricingTools(reader=reader, writer=writer, pms="lodgify")


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

    monkeypatch.setattr(config, "EXPIRY_SEMANTICS_VERIFIED", True)
    monkeypatch.setattr(config, "DELETE_ENDPOINT_VERIFIED", True)


# -- staleness -------------------------------------------------------------


def test_a_price_change_after_the_recommendation_refuses_execution(monkeypatch):
    verified(monkeypatch)

    reader = FakeReader(price=200.0)

    stamp = current_fingerprint(reader)

    reader.price = 235.0  # repriced between recommendation and approval

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
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
    verified(monkeypatch)

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


def test_stale_pricelabs_data_refuses_execution(monkeypatch):
    verified(monkeypatch)

    old = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=40)
    ).isoformat()

    reader = FakeReader(refreshed=old)

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint="whatever",
        reason="test",
        proposed_price=190.0,
    )

    assert result["refusal"] == "STALE_DATA"
    assert writer.calls == []


def test_provider_unavailable_refuses_execution(monkeypatch):
    verified(monkeypatch)

    reader = FakeReader(fail=True)

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
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


def test_an_approved_action_sends_exactly_one_write(monkeypatch):
    enable(monkeypatch)

    reader = FakeReader(price=200.0)

    stamp = current_fingerprint(reader)

    writer = RecordingWriter()

    result = tools(reader, writer).apply_pricing_action(
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


def test_a_provider_refusal_is_a_clean_confirmed_failure(monkeypatch):
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

    result = tools(reader, writer).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="LOWER",
        fingerprint=stamp,
        reason="test",
        proposed_price=190.0,
    )

    assert result["outcome"] == WriteOutcome.CONFIRMED_FAILED.value
    assert result["needs_human"] is False


def test_an_ambiguous_send_is_unknown_and_is_never_retried(monkeypatch):
    enable(monkeypatch)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    writer = RecordingWriter(raises=PriceLabsUnavailable("timeout"))

    result = tools(reader, writer).apply_pricing_action(
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


def test_the_kill_switch_is_off_unless_exactly_true(monkeypatch):
    from app.pricing_config import writes_enabled

    for value in ("", "false", "1", "yes", "TRUE ", "on"):
        monkeypatch.setenv("ENABLE_PRICING_WRITES", value)

        assert writes_enabled() is (value.strip().lower() == "true")


def test_a_disabled_switch_surfaces_as_a_refusal_not_a_crash(monkeypatch):
    verified(monkeypatch)

    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    real = PriceLabsWriteClient(reader=reader, api_key_provider=lambda: "k")

    result = tools(reader, real).apply_pricing_action(
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


def install_pricing_tool(api, writer, reader=None):
    """Register a recording pricing tool on the running app.

    Mirrors production wiring: DANGEROUS and not model-callable, so approval is
    still required and the model still cannot see it.
    """
    from app.tool_setup import apply_pricing_action_tool

    reader = reader or FakeReader()

    tool = apply_pricing_action_tool(tools(reader, writer))

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
    verified(monkeypatch)

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


def test_a_fixed_price_write_is_blocked_while_expiry_is_unverified(monkeypatch):
    """Approval authorises a change; it cannot authorise an untested assumption.

    `lead_time_expiry` was accepted and echoed back by PriceLabs on the first
    live write, which proves persistence and nothing about expiry. Until that
    is settled empirically, a fixed-price write could strand a permanent pin.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    reader = FakeReader()

    stamp = current_fingerprint(reader)

    writer = RecordingWriter()

    for action, price in (("LOWER", 190.0), ("RAISE", 215.0)):
        result = tools(reader, writer).apply_pricing_action(
            listing_id=BUNKERS,
            stay_date="2026-09-20",
            action=action,
            fingerprint=stamp,
            reason="test",
            proposed_price=price,
        )

        assert result["refusal"] == "UNVERIFIED_BEHAVIOUR"
        assert "lead_time_expiry" in result["message"]

    assert writer.calls == [], "no write may reach PriceLabs while this is open"


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
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    reader = FakeReader(fail=True)  # any read would raise

    result = tools(reader, RecordingWriter()).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date="2026-09-20",
        action="RAISE",
        fingerprint="x",
        reason="test",
        proposed_price=215.0,
    )

    assert result["refusal"] == "UNVERIFIED_BEHAVIOUR"


def test_only_the_verified_behaviour_is_unlocked():
    """Each flag reflects exactly what has been proven against the provider.

    DELETE was verified live on 2026-09-04. The fixed-price lifecycle was not,
    so LOWER and RAISE stay shut until the 2026-09-18 expiry check settles it.
    """
    from app.pricing_config import (
        DELETE_ENDPOINT_VERIFIED,
        EXPIRY_SEMANTICS_VERIFIED,
        unverified_reason,
    )

    assert DELETE_ENDPOINT_VERIFIED is True
    assert EXPIRY_SEMANTICS_VERIFIED is False

    assert unverified_reason("REMOVE_PIN") is None
    assert unverified_reason("LOWER") is not None
    assert unverified_reason("RAISE") is not None


def test_the_block_is_surfaced_on_the_recommendation(monkeypatch):
    """The console must say so before a person spends a decision on it."""
    r = rec(PriceAction.RAISE, 215.0)

    from app.pricing_policy import to_payload

    payload = to_payload(r)

    assert payload["actionable"] is True
    assert payload["blocked_reason"] is not None
    assert "lead_time_expiry" in payload["blocked_reason"]


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
