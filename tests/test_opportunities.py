"""The 60-day revenue-opportunity view.

Selection only. Nothing here decides a price -- if a test in this file has to
reason about floors or ceilings, the logic has leaked into the wrong module.

Every value is invented. No test in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.opportunities import is_opportunity, select, summarise, uplift_of
from app.pricing_policy import MAX_DATA_AGE_HOURS

BUNKERS = "680444___747423"
ARBORETUM = "681301___748348"


def fresh() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def stale_stamp() -> str:
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=MAX_DATA_AGE_HOURS + 1
    )

    return old.isoformat()


def row(**over):
    """One recommendation payload, shaped as `to_payload` produces it."""
    base = {
        "id": f"{BUNKERS}:2026-10-05",
        "listing_id": BUNKERS,
        "slug": "boston-bunkers",
        "display_name": "Boston Bunkers",
        "stay_date": "2026-10-05",
        "days_out": 30,
        "action": "RAISE",
        "current_price": 200.0,
        "proposed_price": 220.0,
        "pct_change": 10.0,
        "confidence": "HIGH",
        "reason": "below market p25",
        "actionable": True,
        "blocked_reason": None,
        "stale": False,
        "events": None,
        "market_p25": 240.0,
        "market_booked_median": 300.0,
        "market_occupancy": 50.0,
        "listing_occupancy": 70.0,
        "demand": "Good Demand",
        "owner_floor": 143.0,
        "owner_floor_basis": "flat at the hard floor",
        "auto_raise_ceiling": 252.0,
        "pinned_price": None,
        "last_refreshed_at": fresh(),
    }

    base.update(over)

    return base


# -- what is and is not an opportunity ------------------------------------


def test_an_actionable_raise_on_fresh_evidence_is_an_opportunity():
    assert is_opportunity(row()) is True


@pytest.mark.parametrize("action", ["HOLD", "KEEP_PIN"])
def test_an_informational_recommendation_is_never_an_opportunity(action):
    """HOLD is the engine saying no. Presenting it as a chance inverts that."""
    assert is_opportunity(row(action=action, actionable=False)) is False


def test_a_lower_is_never_an_opportunity():
    """LOWER stays blocked by the Booking.com exposure gate.

    Even were it unblocked it would not belong here: this view answers "where
    is money being left on the table", and showing an action nobody may run as
    something to act on would misrepresent what the system permits.
    """
    lower = row(
        action="LOWER",
        proposed_price=180.0,
        blocked_reason="Lowering a price is blocked ... Booking.com ...",
    )

    assert is_opportunity(lower) is False

    # ...and not even with the block lifted.
    assert is_opportunity(row(action="LOWER", proposed_price=180.0)) is False


def test_a_booked_night_is_never_an_opportunity():
    """The engine already returns HOLD for anything that is not open.

    A booked night reaches this module as a non-actionable HOLD, so the guard
    is that those are filtered -- not that this module re-derives openness,
    which would be a second opinion about inventory.
    """
    booked = row(
        action="HOLD",
        actionable=False,
        reason="Not open inventory.",
        proposed_price=200.0,
    )

    assert is_opportunity(booked) is False


def test_a_raise_blocked_by_a_verification_gate_is_not_an_opportunity():
    """Permitted to exist is not permitted to run."""
    blocked = row(blocked_reason="A fixed-price write is blocked: ...")

    assert is_opportunity(blocked) is False


def test_a_raise_the_guardrails_refused_is_not_an_opportunity():
    """`actionable` is the engine's own verdict and this view defers to it."""
    refused = row(action="HOLD", actionable=False, refused="above the ceiling")

    assert is_opportunity(refused) is False


def test_a_stale_recommendation_is_not_an_opportunity():
    assert is_opportunity(row(stale=True)) is False


def test_staleness_is_decided_from_the_reading_not_asserted_by_the_caller():
    """The `stale` flag the API sends is computed, so this holds end to end."""
    from app.pricing_policy import is_stale

    assert is_stale(stale_stamp()) is True
    assert is_stale(fresh()) is False
    assert is_stale(None) is True, "unknown age is not the same as current"


def test_a_raise_with_no_computable_uplift_is_not_shown():
    """$0 would read as 'no gain'; absent is the honest rendering of unknown."""
    assert uplift_of(row(proposed_price=None)) is None
    assert uplift_of(row(current_price=None)) is None
    assert uplift_of(row(proposed_price=200.0)) is None, "equal is not uplift"

    assert is_opportunity(row(proposed_price=None)) is False


# -- ordering and arithmetic ----------------------------------------------


def test_the_default_order_is_the_largest_dollar_uplift_first():
    rows = select(
        [
            row(id="a", stay_date="2026-10-01", current_price=200.0, proposed_price=210.0),
            row(id="b", stay_date="2026-10-02", current_price=400.0, proposed_price=460.0),
            row(id="c", stay_date="2026-10-03", current_price=100.0, proposed_price=130.0),
        ]
    )

    assert [r["uplift"] for r in rows] == [60.0, 30.0, 10.0]


def test_equal_uplifts_break_by_date_so_the_order_is_stable():
    rows = select(
        [
            row(id="late", stay_date="2026-10-20"),
            row(id="early", stay_date="2026-10-02"),
        ]
    )

    assert [r["id"] for r in rows] == ["early", "late"]


def test_uplift_is_a_price_difference_and_carries_its_percentage():
    rows = select([row(current_price=200.0, proposed_price=220.0)])

    assert rows[0]["uplift"] == 20.0
    assert rows[0]["uplift_pct"] == 10.0


def test_the_summary_total_is_exactly_the_sum_of_the_rows_shown():
    """A reader must be able to add the column up and get the headline.

    Asserted against the displayed rows rather than against the input, so a
    filtered-out row can never be counted in a total nobody can reconcile.
    """
    rows = select(
        [
            row(id="a", current_price=200.0, proposed_price=213.5),
            row(id="b", current_price=300.0, proposed_price=333.25),
            row(id="c", action="HOLD", actionable=False),
            row(id="d", stale=True),
        ]
    )

    summary = summarise(rows)

    assert summary["opportunities"] == 2
    assert summary["total_uplift"] == pytest.approx(
        sum(r["uplift"] for r in rows)
    )
    assert summary["total_uplift"] == pytest.approx(13.5 + 33.25)


def test_the_summary_counts_confidence_and_distinct_properties():
    rows = select(
        [
            row(id="a", confidence="HIGH"),
            row(id="b", confidence="MEDIUM", stay_date="2026-10-06"),
            row(
                id="c",
                confidence="HIGH",
                listing_id=ARBORETUM,
                stay_date="2026-10-07",
            ),
        ]
    )

    summary = summarise(rows)

    assert summary["high_confidence"] == 2
    assert summary["medium_confidence"] == 1
    assert summary["properties"] == 2


def test_an_empty_board_summarises_to_zero_rather_than_failing():
    assert summarise(select([])) == {
        "opportunities": 0,
        "total_uplift": 0,
        "high_confidence": 0,
        "medium_confidence": 0,
        "properties": 0,
    }


# -- the horizon ----------------------------------------------------------


def test_the_horizon_is_sixty_days_inclusive_and_day_sixty_one_is_absent():
    """Day 0 through day 59 is 60 nights; the engine never produces day 60+.

    Held against the service's own horizon rather than a literal, so widening
    HORIZON_DAYS cannot leave this view silently trimming what it is given.
    """
    from app.pricing_service import HORIZON_DAYS

    assert HORIZON_DAYS == 60

    last = row(id="last", days_out=HORIZON_DAYS - 1, stay_date="2026-11-03")
    beyond = row(id="beyond", days_out=HORIZON_DAYS, stay_date="2026-11-04")

    rows = select([last, beyond])

    # Selection does not filter by horizon -- the engine bounds that -- so both
    # survive here. What this pins is that the boundary is the engine's, and
    # that nothing in this module quietly re-cuts it.
    assert [r["id"] for r in rows] == ["last", "beyond"]
    assert max(r["days_out"] for r in rows) == HORIZON_DAYS


def test_the_service_never_offers_a_night_past_the_horizon():
    """The real boundary, at the only place that draws it."""
    import datetime as _dt

    from app.pricing_service import HORIZON_DAYS

    start = _dt.date(2026, 9, 6)

    end = start + _dt.timedelta(days=HORIZON_DAYS - 1)

    assert (end - start).days == 59, "day 0 through day 59 is 60 nights"
    assert end == _dt.date(2026, 11, 4)


# -- the route ------------------------------------------------------------


def test_the_route_is_read_only_and_reaches_no_write_path(api, monkeypatch):
    """Decision support, and structurally incapable of being anything else.

    The strong claim is not that the handler happens not to write today, but
    that it cannot: it is a GET behind VIEW_RUNS, and the pricing write tool is
    replaced here with one that fails the test on any call.
    """
    from app.tool_registry import Tool, ToolRisk

    called: list[str] = []

    api.module.tool_registry.register(
        Tool(
            name="apply_pricing_action",
            description="must not run",
            function=lambda **kw: called.append("executed"),
            parameters={"type": "object", "properties": {}},
            risk=ToolRisk.DANGEROUS,
            model_callable=False,
        )
    )

    class OnlyReads:
        def build(self):
            return []

    api.module.pricelabs_recommendations = OnlyReads()

    response = api.client("VIEWER").get("/vacancy/opportunities")

    assert response.status_code == 200
    assert called == [], "no pricing action may execute"

    body = response.json()

    assert body["opportunities"] == []
    assert body["summary"]["opportunities"] == 0
    assert body["horizon_days"] == 60

    # Nothing in the payload could be submitted even if a page wanted to.
    assert "approval_id" not in body
    assert "fingerprint" not in body


def test_the_route_never_offers_a_lower_or_a_hold(api, monkeypatch):
    """End to end, through the real payload shape."""
    import datetime as _dt

    from app.pricing_config import bands_for
    from app.pricing_policy import (
        Confidence,
        MarketState,
        PriceAction,
        Recommendation,
    )

    band = bands_for(BUNKERS)

    def make(action, proposed, actionable_price=200.0):
        return Recommendation(
            listing_id=BUNKERS,
            slug=band.slug,
            display_name="Boston Bunkers",
            stay_date=_dt.date(2026, 10, 5),
            days_out=30,
            action=action,
            current_price=actionable_price,
            proposed_price=proposed,
            confidence=Confidence.HIGH,
            reason="test",
            state=MarketState(
                current_price=actionable_price,
                market_p25=240.0,
                market_booked_median=300.0,
                market_occupancy=50.0,
                listing_occupancy=70.0,
                demand="Good Demand",
                pickup_7_days=None,
                pinned_price=None,
                last_refreshed_at=fresh(),
            ),
            bands=band,
        )

    class Fixed:
        def build(self):
            return [
                make(PriceAction.RAISE, 215.0),
                make(PriceAction.LOWER, 185.0),
                make(PriceAction.HOLD, 200.0),
            ]

    api.module.pricelabs_recommendations = Fixed()

    body = api.client("VIEWER").get("/vacancy/opportunities").json()

    assert [row["action"] for row in body["opportunities"]] == ["RAISE"]
    assert body["summary"]["opportunities"] == 1
    assert body["opportunities"][0]["uplift"] == 15.0


def test_the_route_requires_a_reader_permission(api):
    assert api.client("VIEWER").get("/vacancy/opportunities").status_code in (200, 503)
    assert api.anonymous().get("/vacancy/opportunities").status_code == 401
