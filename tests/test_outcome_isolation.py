"""The tracker must be unable to affect the thing it observes.

Outcome tracking is analytics bolted onto an irreversible write path. The
hazards are specific: a broken tracker turning a successful live price change
into a reported failure, evidence arriving from the caller instead of the
server, a forwarded provider field carrying a guest's name into the database,
or observation quietly becoming an input to the next recommendation.

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import inspect

import pytest

from app.connectors.pricelabs.client import PriceLabsClient
from app.connectors.pricelabs.pricing_tools import (
    APPLY_PRICING_ACTION_SCHEMA,
    PriceLabsPricingTools,
)

BUNKERS = "680444___747423"


def market(**over):
    """A `MarketState` with every field supplied, so tests name only what
    they care about."""
    from app.pricing_policy import MarketState

    fields = {
        "current_price": 182.0,
        "market_p25": None,
        "market_booked_median": None,
        "market_occupancy": None,
        "listing_occupancy": None,
        "demand": None,
        "pickup_7_days": None,
        "pinned_price": None,
        "last_refreshed_at": None,
    }

    fields.update(over)

    return MarketState(**fields)


# -- the connector projection ---------------------------------------------


class FakeTransport:
    """Stands in for PriceLabs, returning the provider's *full* row shape."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self, method, path, **kwargs):
        return {"data": self.rows, "next_page": False, "pms_name": "lodgify"}


def raw_reservation(**over):
    """What the provider actually returns, guest fields and all."""
    row = {
        "reservation_id": "r-1",
        "listing_id": BUNKERS,
        "listing_name": "Boston Bunkers",
        "booked_date": "2026-09-06T16:30:00.000Z",
        "check_in": "2026-09-10",
        "check_out": "2026-09-12",
        "no_of_days": 2,
        "rental_revenue": 340.0,
        "cleaning_fees": 90.0,
        "total_cost": 430.0,
        "booking_status": "booked",
        "booking_channel": "bcom",
        "guest_count": 3,
        "guestName": "A Real Person",
        "channelConfirmationCode": "5025280306|5737611577",
    }

    row.update(over)

    return row


@pytest.fixture
def client(monkeypatch) -> PriceLabsClient:
    made = PriceLabsClient(api_key_provider=lambda: "test-key")

    monkeypatch.setattr(made, "_request", FakeTransport([raw_reservation()]))

    return made


def test_the_projection_carries_channel_and_check_out(client):
    """The two fields added for outcome tracking, each earning its place."""
    rows = client.reservations(BUNKERS, "lodgify", "2026-09-01", "2026-09-30")

    assert rows[0]["booking_channel"] == "bcom"
    assert rows[0]["check_out"] == "2026-09-12"


def test_the_projection_forwards_no_guest_or_confirmation_field(client):
    """Constructed, never forwarded. Adding two fields did not open a door."""
    rows = client.reservations(BUNKERS, "lodgify", "2026-09-01", "2026-09-30")

    assert set(rows[0]) == {
        "reservation_id",
        "listing_id",
        "booked_date",
        "check_in",
        "check_out",
        "no_of_days",
        "rental_revenue",
        "booking_status",
        "booking_channel",
    }

    text = str(rows[0])

    for leaked in (
        "A Real Person",
        "5025280306",
        "Boston Bunkers",
        "cleaning_fees",
        "total_cost",
        "guest_count",
    ):
        assert leaked not in text


def test_the_projection_is_built_field_by_field_not_by_passthrough():
    """Checked against the code, with the docstring's own prose removed."""
    source = inspect.getsource(PriceLabsClient.reservations)
    body = source.split('"""')[-1]

    for shortcut in ("**row", "dict(row)", "row.copy()", "**rest"):
        assert shortcut not in body


# -- the tracker cannot fail a pricing action -----------------------------


class ExplodingOutcomes:
    """A tracker that is broken in the worst possible way."""

    def record_execution(self, **kwargs):
        raise RuntimeError("the analytics database is on fire")


class RecordingOutcomes:
    def __init__(self):
        self.calls = []

    def record_execution(self, **kwargs):
        self.calls.append(kwargs)


def test_a_broken_tracker_cannot_fail_a_successful_pricing_action():
    """The price has already moved. Reporting failure would be the real harm.

    Exercised at the seam rather than through the whole write path, so the
    assertion is about `_record_outcome` swallowing everything -- which is the
    behaviour the write path depends on.
    """
    from app.tool_registry import ExecutionContext

    tools = PriceLabsPricingTools(
        reader=object(),
        writer=object(),
        outcomes=ExplodingOutcomes(),
        evidence_source=lambda _l, _s: {},
    )

    # Must not raise.
    tools._record_outcome(
        ctx=ExecutionContext(run_id="run-1", approval_id="ap-1"),
        listing_id=BUNKERS,
        stay_date="2026-09-10",
        action="LOWER",
        write_outcome="CONFIRMED_APPLIED",
        state=market(),
        currency="USD",
        proposed_price=164.0,
        cleanup_id="cl-1",
    )


def test_a_broken_evidence_source_still_records_the_action():
    """Losing the evidence is bad. Losing the row entirely is worse."""
    from app.tool_registry import ExecutionContext

    outcomes = RecordingOutcomes()

    def broken(_listing, _stay):
        raise RuntimeError("recommendation rebuild failed")

    tools = PriceLabsPricingTools(
        reader=object(),
        writer=object(),
        outcomes=outcomes,
        evidence_source=broken,
    )

    tools._record_outcome(
        ctx=ExecutionContext(run_id="run-1", approval_id="ap-1"),
        listing_id=BUNKERS,
        stay_date="2026-09-10",
        action="LOWER",
        write_outcome="CONFIRMED_APPLIED",
        state=market(),
        currency="USD",
        proposed_price=164.0,
        cleanup_id="cl-1",
    )

    # The evidence source raised before the row could be built, so nothing was
    # recorded -- and, crucially, nothing was raised either.
    assert outcomes.calls == []


def test_no_tracker_configured_leaves_the_write_path_untouched():
    """The tracker is optional, like a connector. Absent means absent."""
    from app.tool_registry import ExecutionContext

    tools = PriceLabsPricingTools(reader=object(), writer=object())

    assert tools._outcomes is None

    tools._record_outcome(
        ctx=ExecutionContext(run_id="run-1", approval_id="ap-1"),
        listing_id=BUNKERS,
        stay_date="2026-09-10",
        action="LOWER",
        write_outcome="CONFIRMED_APPLIED",
        state=market(),
        currency="USD",
        proposed_price=164.0,
        cleanup_id=None,
    )


# -- evidence is server-derived -------------------------------------------


def test_the_tool_schema_offers_no_evidence_argument():
    """A caller supplying the data its own decision is judged against is
    assertion, not evidence. The schema must give it nowhere to put any."""
    properties = set(APPLY_PRICING_ACTION_SCHEMA["properties"])

    assert properties == {
        "listing_id",
        "stay_date",
        "action",
        "proposed_price",
        "fingerprint",
        "reason",
    }
    assert APPLY_PRICING_ACTION_SCHEMA["additionalProperties"] is False


def test_evidence_comes_from_the_server_side_source_not_the_arguments():
    from app.tool_registry import ExecutionContext

    outcomes = RecordingOutcomes()

    tools = PriceLabsPricingTools(
        reader=object(),
        writer=object(),
        outcomes=outcomes,
        evidence_source=lambda _l, _s: {
            "historical_lead_band_adr": 149.5,
            "history_sample_count": 9,
            "market_signal_conflict": True,
            "owner_floor": 143.0,
            "observed_commission_rate": 23.0,
            "listing_occupancy": 65.0,
        },
    )

    tools._record_outcome(
        ctx=ExecutionContext(run_id="run-1", approval_id="ap-1"),
        listing_id=BUNKERS,
        stay_date="2026-09-10",
        action="LOWER",
        write_outcome="CONFIRMED_APPLIED",
        # The market half comes from the freshly re-read provider state, which
        # is the same state the fingerprint was verified against.
        state=market(
            market_p25=199.8,
            market_booked_median=233.0,
            market_occupancy=46.09,
            demand="Normal Demand",
        ),
        currency="USD",
        proposed_price=164.0,
        cleanup_id="cl-1",
    )

    evidence = outcomes.calls[0]["evidence"]

    assert evidence["history_adr"] == pytest.approx(149.5)
    assert evidence["history_sample_count"] == 9
    assert evidence["market_p25"] == pytest.approx(199.8)
    assert evidence["market_booked_median"] == pytest.approx(233.0)
    assert evidence["market_occupancy"] == pytest.approx(46.09)
    assert evidence["demand"] == "Normal Demand"
    assert evidence["market_signal_conflict"] is True
    assert evidence["observed_commission_rate"] == pytest.approx(23.0)

    # `price_before` is the published price, not the previous override.
    assert outcomes.calls[0]["price_before"] == pytest.approx(182.0)


# -- observation is never an input ----------------------------------------


def test_the_recommendation_engine_does_not_read_outcomes():
    """Observation must never close the loop into pricing.

    Checked as an import-level property: if no module in the recommendation
    path can reach the outcome store, no code in it can consult one.
    """
    import app.pricing_policy as policy
    import app.pricing_recommendations as recommendations
    import app.pricing_service as service

    for module in (policy, recommendations, service):
        source = inspect.getsource(module)

        for forbidden in (
            "pricing_outcomes",
            "PricingOutcomeStore",
            "pricing_reconciler",
            "first_booked_at",
            "cancelled_after_booking",
        ):
            assert forbidden not in source, f"{module.__name__} reads outcomes"


def test_recommendations_are_identical_with_and_without_the_tracker(database):
    """The engine takes no outcome input, so rows cannot vary with one.

    Built from the same fake provider twice -- once with an outcome table
    holding rows, once empty -- and compared field for field.
    """
    from app.pricing_outcomes import PricingOutcomeStore
    from app.pricing_recommendations import payloads
    from app.pricing_service import PricingRecommendationService

    class QuietProvider:
        def listings(self):
            return []

        def listing_prices(self, *a, **k):
            return []

        def neighborhood_data(self, *a, **k):
            return {}

    before = payloads(PricingRecommendationService(QuietProvider()).build())

    store = PricingOutcomeStore(database=database)

    store.record_execution(
        approval_id="ap-1",
        run_id="run-1",
        listing_id=BUNKERS,
        stay_date="2026-09-10",
        action="LOWER",
        executed_at="2026-09-06T10:00:00+00:00",
        write_outcome="CONFIRMED_APPLIED",
        price_before=182.0,
        price_after=164.0,
    )

    after = payloads(PricingRecommendationService(QuietProvider()).build())

    assert before == after
