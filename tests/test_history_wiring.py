"""One history load per recommendation build, shared by both routes.

The hazard is drift. Two routes each fetching their own history would work
until one of them stopped, and the symptom would be LOWER appearing on one
screen and not the other -- which reads as a pricing decision rather than a
plumbing bug. So `build_recommendations` is the only path, and these hold that.

A note on what "loaded once" can mean here: `/v1/reservation_data` requires a
`listing_id`, so one history load necessarily makes one paginated fetch per
configured listing. What must not happen is a second *load* -- per route, per
night, or per anything else. That is the invariant asserted, rather than a
single provider request, which this API cannot offer.

Every value is invented. No test in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.pricing_config import BANDS
from app.pricing_policy import Confidence, MarketState, PriceAction, Recommendation

BUNKERS = "680444___747423"


class CountingRecommendations:
    """Stands in for the service, recording what history it was handed."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else []

    def build(self, history=None):
        self.calls.append(history)

        return self.result


@pytest.fixture
def wired(api, monkeypatch):
    """The app module with a counting service and a counting history loader."""
    module = api.module

    service = CountingRecommendations()

    module.pricelabs_recommendations = service

    loads = []

    def counting_load_history(client, listing_ids, *a, **kw):
        loads.append(list(listing_ids))

        return {BUNKERS: [(30, 200.0)]}, None

    monkeypatch.setattr(module, "load_history", counting_load_history)

    # A client that is merely non-None; the patched loader never calls it.
    monkeypatch.setattr(module, "pricelabs_client", object())

    return module, service, loads


def test_a_build_loads_history_exactly_once(wired):
    module, service, loads = wired

    module.build_recommendations()

    assert len(loads) == 1
    assert len(service.calls) == 1


def test_the_service_receives_one_completed_history_map(wired):
    """Not a generator, not a partial map, not per-night lookups."""
    module, service, _ = wired

    module.build_recommendations()

    handed = service.calls[0]

    assert handed == {BUNKERS: [(30, 200.0)]}
    assert isinstance(handed, dict)


def test_history_is_requested_for_every_configured_listing_exactly_once(wired):
    module, _, loads = wired

    module.build_recommendations()

    requested = loads[0]

    assert requested == [band.listing_id for band in BANDS]
    assert len(requested) == len(set(requested)) == 7


def test_neither_route_performs_its_own_extra_history_fetch(api, wired):
    """Both screens, one load each, and no route-level fetching of its own."""
    _, service, loads = wired

    assert api.client("VIEWER").get("/vacancy/recommendations").status_code == 200

    assert len(loads) == 1
    assert len(service.calls) == 1

    assert api.client("VIEWER").get("/vacancy/opportunities").status_code == 200

    assert len(loads) == 2, "one load per build, not one per build plus extras"
    assert len(service.calls) == 2


def test_no_per_night_history_fetch_occurs(api, monkeypatch):
    """Sixty nights across seven listings is one load, not four hundred.

    The counting client records every listing-level fetch, so a per-night or
    per-recommendation lookup would show up as an explosion in that count.
    """
    module = api.module

    fetches = []

    class CountingClient:
        def reservations(self, listing_id, pms, start, end):
            fetches.append(listing_id)

            return []

    monkeypatch.setattr(module, "pricelabs_client", CountingClient())

    service = CountingRecommendations()

    module.pricelabs_recommendations = service

    module.build_recommendations()

    assert len(fetches) == len(BANDS) == 7
    assert sorted(fetches) == sorted(band.listing_id for band in BANDS)


def test_a_history_failure_hands_the_service_none_not_an_empty_map(
    api,
    monkeypatch,
    caplog,
):
    """The failure path, end to end.

    `None` reaches `build`, which behaves exactly as it did before history
    existed: RAISE and HOLD still work and LOWER stays unreachable. An empty
    map would look identical to "no property has bookings" and would suppress
    a price reduction on the strength of a network error.
    """
    module = api.module

    class FailingClient:
        def reservations(self, *a, **kw):
            raise PriceLabsUnavailable("PriceLabs answered 503")

    monkeypatch.setattr(module, "pricelabs_client", FailingClient())

    service = CountingRecommendations()

    module.pricelabs_recommendations = service

    with caplog.at_level("WARNING"):
        module.build_recommendations()

    assert service.calls == [None]
    assert any("history unavailable" in r.message.lower() for r in caplog.records), (
        "a board with no LOWER on it must be explainable"
    )


def test_an_unconfigured_connector_still_builds_without_history(api, monkeypatch):
    module = api.module

    monkeypatch.setattr(module, "pricelabs_client", None)

    service = CountingRecommendations()

    module.pricelabs_recommendations = service

    module.build_recommendations()

    assert service.calls == [None]


# -- behaviour preservation ------------------------------------------------
#
# History exists to make the LOWER branch reachable. It must not move anything
# else, and "anything else" is asserted rather than assumed.


def night(action, listing_id=BUNKERS, price=200.0, proposed=None, days_out=30):
    from app.pricing_config import bands_for

    band = bands_for(listing_id)

    return Recommendation(
        listing_id=listing_id,
        slug=band.slug,
        display_name=band.display_name,
        stay_date=datetime.date(2026, 10, 5),
        days_out=days_out,
        action=action,
        current_price=price,
        proposed_price=proposed if proposed is not None else price,
        confidence=Confidence.MEDIUM,
        reason="test",
        state=MarketState(
            current_price=price,
            market_p25=240.0,
            market_booked_median=300.0,
            market_occupancy=40.0,
            listing_occupancy=67.0,
            demand="Normal Demand",
            pickup_7_days=None,
            pinned_price=None,
            last_refreshed_at=None,
        ),
        bands=band,
    )


def build_twice(monkeypatch, provider, history):
    """The same engine over one provider state, without and with history."""
    from app.pricing_service import PricingRecommendationService

    service = PricingRecommendationService(provider)

    without = {
        (r.listing_id, r.stay_date.isoformat()): r for r in service.build()
    }
    with_history = {
        (r.listing_id, r.stay_date.isoformat()): r
        for r in service.build(history=history)
    }

    return without, with_history


class Provider:
    """A small portfolio: one night that should lower, and several that must not."""

    STAY = "2026-10-05"

    def __init__(self, price=228.0, days_out=5, demand="Normal Demand",
                 booking_status="", override=None, listing=BUNKERS,
                 market_occupancy=50.0):
        self.price = price
        self.days_out = days_out
        self.demand = demand
        self.booking_status = booking_status
        self.override = override
        self.listing = listing
        # Above `DATE_OCC_STRONG` by default, so the date registers as strong
        # and the RAISE rule can qualify. Without that the near-term branch
        # falls through to HOLD and the precedence under test never arises.
        self.market_occupancy = market_occupancy

    def _stay(self):
        return (
            datetime.datetime.now(datetime.UTC).date()
            + datetime.timedelta(days=self.days_out)
        ).isoformat()

    def listings(self):
        from app.pricing_config import bands_for

        band = bands_for(self.listing)

        return [{
            "id": self.listing, "name": band.display_name, "pms": "lodgify",
            "currency": "USD", "no_of_bedrooms": 3,
            "occupancy_next_60": 67, "market_occupancy_next_60": 40,
        }]

    def listing_prices(self, pairs, start, end):
        return [{
            "id": self.listing,
            "last_refreshed_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "data": [{
                "date": self._stay(), "price": self.price,
                "demand_desc": self.demand, "booking_status": self.booking_status,
                "unbookable": 0, "min_stay": 2,
            }],
        }]

    def overrides(self, listing_id, pms):
        if self.override is None:
            return []

        return [{"date": self._stay(), "price": self.override}]

    def neighborhood_data(self, listing_id, pms):
        # The provider's real column-oriented shape: a date axis in `X_values`
        # and one series per label in `Y_values`, keyed by bedroom Category.
        #
        # Served under every band on purpose: Bunkers is analysed at 2 through
        # its `bedrooms_override` while the provider reports 3, and a fake that
        # answered only one would make the market reference vanish -- which is
        # a "No market reference" HOLD, not the branch under test.
        labels = ["25th Percentile", "50th Percentile", "Median Booked Price"]

        entry = {
            "X_values": [self._stay()],
            "Y_values": [[206.0], [260.0], [300.0]],
        }

        occupancy = {
            "X_values": [self._stay()],
            "Y_values": [[[self.market_occupancy]]],
        }

        return {
            "Future Percentile Prices": {
                "Labels": labels,
                "Category": {str(n): entry for n in range(1, 6)},
            },
            "Future Occ/New/Canc": {
                "Labels": ["Occupancy"],
                "Category": {str(n): occupancy for n in range(1, 6)},
            },
        }


def actions(provider, history=None):
    from app.pricing_service import PricingRecommendationService

    return {
        (r.listing_id, r.stay_date.isoformat()): r
        for r in PricingRecommendationService(provider).build(history=history)
    }


#: Nine bookings in the 4-7d band around $149.50 -- enough for the engine's
#: `len(samples) >= 3` rule, and far enough below $228 to clear `HIST_OVER`.
LOWER_HISTORY = {BUNKERS: [(d, adr) for d in (4, 5, 6, 7, 4, 5, 6, 7, 5)
                           for adr in (149.5,)]}


#: Nine bookings in the 4-7d band at $149.50 -- past the engine's
#: `len(samples) >= 3` rule and far enough below $228 to clear `HIST_OVER`.
def band_history(listing=BUNKERS, adr=149.5, n=9, days=(4, 5, 6, 7)):
    return {listing: [(days[i % len(days)], adr) for i in range(n)]}


def test_a_qualifying_market_night_raises_when_there_is_no_history():
    """Requirement 1: the RAISE this precedence will later pre-empt."""
    row = next(iter(actions(Provider(price=180.0, days_out=5)).values()))

    assert row.action is PriceAction.RAISE
    assert "below the market p25" in row.reason
    assert row.market_signal_conflict is False


def test_near_term_history_pre_empts_a_simultaneous_raise():
    """Requirements 2 and 3: the live behaviour, made deliberate.

    The same night, same provider state. Without history it raises on the comp
    set; with history it lowers on what this property has actually converted
    at. Both rules genuinely qualify -- the market is asking more than this
    unit has ever achieved -- and near arrival under weak demand, its own
    history wins.
    """
    provider = Provider(price=180.0, days_out=5)

    without = next(iter(actions(provider).values()))
    with_history = next(iter(actions(provider, band_history()).values()))

    assert without.action is PriceAction.RAISE
    assert with_history.action is PriceAction.LOWER

    assert with_history.market_signal_conflict is True
    assert without.market_signal_conflict is False

    assert "takes precedence" in with_history.reason
    assert "close to arrival and demand is weak" in with_history.reason


def test_the_conflict_flag_is_absent_when_only_one_rule_qualifies():
    """A LOWER on a night the market would not have raised is not a conflict."""
    provider = Provider(price=228.0, days_out=5)

    row = next(iter(actions(provider, band_history()).values()))

    assert row.action is PriceAction.LOWER
    assert row.market_signal_conflict is False, (
        "$228 is above market p25 $206, so the RAISE rule never qualified"
    )
    assert "takes precedence" not in row.reason


@pytest.mark.parametrize("samples", [0, 1, 2])
def test_too_few_samples_leaves_the_raise_alone(samples):
    """Requirement 4. The engine needs >= 3 in the band to form a median."""
    provider = Provider(price=180.0, days_out=5)

    row = next(iter(actions(provider, band_history(n=samples)).values()))

    assert row.action is PriceAction.RAISE
    assert row.market_signal_conflict is False


def test_strong_demand_leaves_the_raise_alone():
    """Requirement 5. Weak demand is part of the LOWER rule, not a detail."""
    provider = Provider(price=180.0, days_out=5, demand="Good Demand")

    row = next(iter(actions(provider, band_history()).values()))

    assert row.action is PriceAction.RAISE


def test_outside_the_near_term_window_history_does_not_pre_empt():
    """Requirement 6. The precedence is about proximity to arrival."""
    import app.pricing_recommendations as prm

    provider = Provider(price=180.0, days_out=prm.NEAR_TERM_DAYS + 1)

    history = {BUNKERS: [(prm.NEAR_TERM_DAYS + 1, 149.5)] * 9}

    row = next(iter(actions(provider, history).values()))

    assert row.action is PriceAction.RAISE


def test_a_price_within_the_history_threshold_leaves_the_raise_alone():
    """Requirement 7. `HIST_OVER` is what makes the gap material."""
    import app.pricing_recommendations as prm

    # Just under 1.15x the historical median, so the LOWER rule declines.
    adr = 180.0 / (prm.HIST_OVER - 0.01)

    provider = Provider(price=180.0, days_out=5)

    row = next(iter(actions(provider, band_history(adr=adr)).values()))

    assert row.action is PriceAction.RAISE


def test_history_never_makes_a_sub_floor_lower_actionable():
    """Requirement 8. The guardrail refuses; history does not buy an exception."""
    from app.pricing_config import bands_for

    listing = "681286___748333"  # Harvard: hard floor 338

    band = bands_for(listing)

    provider = Provider(price=band.hard_floor + 1, days_out=5, listing=listing,
                        demand="Low Demand")

    history = {listing: [(5, 100.0)] * 9}

    row = next(iter(actions(provider, history).values()))

    if row.proposed_price is not None and row.is_actionable:
        assert row.proposed_price >= band.hard_floor


def test_a_night_that_stays_raise_keeps_its_exact_proposal():
    """Requirement 9. Where the precedence does not apply, nothing moves."""
    provider = Provider(price=180.0, days_out=5, demand="Good Demand")

    without = next(iter(actions(provider).values()))
    with_history = next(iter(actions(provider, band_history()).values()))

    assert without.action is with_history.action is PriceAction.RAISE
    assert without.proposed_price == with_history.proposed_price
    assert without.reason == with_history.reason


def test_the_historical_adr_is_never_the_proposed_price():
    """Requirement 11. Evidence, not a target.

    $180 against a $149.50 median: the executable step is the cap-safe $162,
    not the median. The whole gap is visible in the reason, never in the price.
    """
    provider = Provider(price=180.0, days_out=5)

    row = next(iter(actions(provider, band_history()).values()))

    assert row.action is PriceAction.LOWER
    assert row.proposed_price == 162
    assert row.proposed_price != 149.5
    assert "$150" in row.reason, "the evidence is stated, not applied"


def test_cap_safe_rounding_still_bounds_a_pre_empting_lower():
    """Requirement 12, on the branch that now actually fires."""
    from app.pricing_config import MAX_CHANGE_PER_RUN

    provider = Provider(price=180.0, days_out=5)

    row = next(iter(actions(provider, band_history()).values()))

    move = abs(row.proposed_price - row.current_price) / row.current_price

    assert move <= MAX_CHANGE_PER_RUN + 1e-9
    assert row.is_actionable


def test_a_booked_night_never_becomes_a_lower_however_rich_the_history():
    provider = Provider(booking_status="booked")

    with_history = actions(provider, LOWER_HISTORY)

    assert next(iter(with_history.values())).action is PriceAction.HOLD


def test_a_lower_proposal_stays_inside_the_per_run_cap():
    """Requirement 20, after cap-safe rounding."""
    from app.pricing_config import MAX_CHANGE_PER_RUN

    row = next(iter(actions(Provider(), LOWER_HISTORY).values()))

    assert row.action is PriceAction.LOWER
    assert row.proposed_price == 206, "228 clamps to 205.20 and rounds up to 206"

    move = abs(row.proposed_price - row.current_price) / row.current_price

    assert move <= MAX_CHANGE_PER_RUN + 1e-9
    assert row.is_actionable, "a cap-safe proposal must survive the guardrail"


def test_a_lower_proposal_never_crosses_the_hard_floor():
    """Requirement 21. The guardrail refuses rather than the price being shaved."""
    from app.pricing_config import bands_for

    band = bands_for(BUNKERS)

    provider = Provider(price=band.hard_floor + 2)

    row = next(iter(actions(provider, LOWER_HISTORY).values()))

    if row.proposed_price is not None:
        assert row.proposed_price >= band.hard_floor or not row.is_actionable


def test_a_proposal_below_the_owner_floor_carries_the_review_note():
    """Requirement 22. Above the hard floor, under the owner floor: ask a person."""
    from app.pricing_config import bands_for

    listing = "681293___748340"  # condo 2nd Floor: hard 189, normal 221

    band = bands_for(listing)

    provider = Provider(price=217.0, days_out=2, listing=listing,
                        demand="Low Demand")

    history = {listing: [(d, 160.5) for d in (0, 1, 2, 3, 1, 2, 3)]}

    row = next(iter(actions(provider, history).values()))

    assert row.action is PriceAction.LOWER
    assert row.proposed_price == 196
    assert band.hard_floor < row.proposed_price < band.normal_floor
    assert any("owner floor" in note for note in row.notes)
    assert any("explicit decision" in note for note in row.notes)


def test_a_proposal_above_the_owner_floor_carries_no_such_note():
    """Bunkers has owner floor == hard floor, so its steps need no exception."""
    from app.pricing_config import bands_for, dynamic_floor

    band = bands_for(BUNKERS)

    row = next(iter(actions(Provider(), LOWER_HISTORY).values()))

    floor = dynamic_floor(band, row.stay_date, row.days_out)

    assert floor is not None
    assert floor[0] == band.hard_floor == 143.0
    assert row.proposed_price > floor[0]
    assert not any("owner floor" in note for note in row.notes)
