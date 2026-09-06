"""`pinned_price` must mean the same thing on both paths that compute it.

`PricingRecommendationService.build()` produces the state a recommendation is
made from; `PriceLabsPricingTools._current_state()` produces the state it is
executed against. Both feed `fingerprint`, so a field either path reads
differently is not a discrepancy that shows up as a wrong number -- it shows up
as a write that is refused as STALE forever.

That is what happened here. `build()` reported the *published* nightly price as
`pinned_price`; `_current_state` reported the *override's* price. On a date
where a pin has diverged from what PriceLabs publishes -- the exact case
REMOVE_PIN exists for -- the two never agreed, and every REMOVE_PIN on such a
date was permanently unexecutable. Three live dates were refused this way.

So the assertion that matters here is **build() against _current_state()**, not
either path round-tripping against itself. A per-path round trip passes happily
while the two disagree, which is why the existing fingerprint test did not
catch this.

Every value is invented. No test in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.connectors.pricelabs.pricing_tools import (
    PriceLabsPricingTools,
    fingerprint_of,
)
from app.pricing_service import PricingRecommendationService

BUNKERS = "680444___747423"
STAY = "2026-09-16"

PUBLISHED = 164.0
OVERRIDE = 180.0


def fresh_stamp() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class TwoPathReader:
    """One provider state, served to whichever path asks.

    Deliberately a single object: the point of the test is that two readers of
    the *same* provider state agree, so giving each path its own fixture would
    quietly assume away the bug.
    """

    def __init__(self, published=PUBLISHED, override_price=OVERRIDE, stay=STAY):
        self.published = published
        self.override_price = override_price
        self.stay = stay

    def listings(self):
        return [
            {
                "id": BUNKERS,
                "name": "Boston Bunkers",
                "pms": "lodgify",
                "currency": "USD",
                "no_of_bedrooms": 3,
                "occupancy_next_60": 67,
                "market_occupancy_next_60": 40,
            }
        ]

    def listing_prices(self, pairs, start, end):
        return [
            {
                "id": BUNKERS,
                "last_refreshed_at": fresh_stamp(),
                "data": [
                    {
                        "date": self.stay,
                        "price": self.published,
                        "demand_desc": "Normal Demand",
                        "booking_status": "",
                        "unbookable": 0,
                        "min_stay": 2,
                    }
                ],
            }
        ]

    def overrides(self, listing_id, pms):
        if self.override_price is _ABSENT:
            return []

        return [{"date": self.stay, "price": self.override_price}]

    def neighborhood_data(self, listing_id, pms):
        return {
            "data": {
                "Category": {
                    "2": {
                        "X_values": [self.stay],
                        "Labels": ["Percentile Prices"],
                        "Y_values": [
                            {
                                "label": "Percentile Prices",
                                "Values": {"25": [195.5], "50": [230.0]},
                            }
                        ],
                    }
                }
            }
        }


_ABSENT = object()


def both_states(reader):
    """The two real paths, over one provider state."""
    built = [
        r
        for r in PricingRecommendationService(reader).build()
        if r.stay_date.isoformat() == reader.stay
    ]

    assert built, "the service produced no recommendation for the stay date"

    execution, _ = PriceLabsPricingTools(
        reader=reader,
        writer=None,
        pms="lodgify",
    )._current_state(BUNKERS, reader.stay)

    return built[0].state, execution


def test_an_override_that_differs_from_the_published_price_reconciles():
    """The live failure, reproduced: published 164, pinned 180.

    Before the fix `build()` reported 164 here and `_current_state` reported
    180, so the fingerprints could never match and the REMOVE_PIN was refused
    as STALE on every attempt.
    """
    recommendation, execution = both_states(TwoPathReader())

    assert recommendation.pinned_price == OVERRIDE
    assert execution.pinned_price == OVERRIDE

    assert recommendation.current_price == PUBLISHED, (
        "the published nightly price is `current_price` and must not move"
    )

    stay = datetime.date.fromisoformat(STAY)

    assert fingerprint_of(BUNKERS, STAY, execution) == fingerprint_of(
        BUNKERS, STAY, recommendation
    )
    assert stay.isoformat() == STAY


def test_an_override_equal_to_the_published_price_reconciles():
    """The case that always worked, held so the fix did not break it."""
    recommendation, execution = both_states(
        TwoPathReader(override_price=PUBLISHED)
    )

    assert recommendation.pinned_price == PUBLISHED
    assert execution.pinned_price == PUBLISHED
    assert fingerprint_of(BUNKERS, STAY, execution) == fingerprint_of(
        BUNKERS, STAY, recommendation
    )


def test_a_night_with_no_override_reports_no_pinned_price():
    recommendation, execution = both_states(
        TwoPathReader(override_price=_ABSENT)
    )

    assert recommendation.pinned_price is None
    assert execution.pinned_price is None
    assert recommendation.current_price == PUBLISHED
    assert fingerprint_of(BUNKERS, STAY, execution) == fingerprint_of(
        BUNKERS, STAY, recommendation
    )


@pytest.mark.parametrize("bad", [None, "", "not-a-number", 0, -5])
def test_an_unreadable_override_price_fails_closed(bad):
    """No pinned price, rather than the published price wearing that name.

    Substituting `current_price` would make the two paths agree -- and the
    agreement would be about a pin that does not exist. Unknown must stay
    unknown, on both sides, so they still reconcile without inventing a value.
    """
    recommendation, execution = both_states(TwoPathReader(override_price=bad))

    assert recommendation.pinned_price is None
    assert execution.pinned_price is None
    assert recommendation.pinned_price != PUBLISHED
    assert fingerprint_of(BUNKERS, STAY, execution) == fingerprint_of(
        BUNKERS, STAY, recommendation
    )


def test_the_two_paths_agree_across_a_range_of_divergences():
    """Not one lucky pair of numbers.

    A fix that happened to work at 164/180 and nowhere else would be no fix,
    so the reconciliation is asserted across the spread of gaps seen live --
    Bunkers 164/180 and 171/188, Harvard 547/600.
    """
    for published, pinned in ((164.0, 180.0), (171.0, 188.0), (547.0, 600.0)):
        recommendation, execution = both_states(
            TwoPathReader(published=published, override_price=pinned)
        )

        assert recommendation.pinned_price == pinned
        assert execution.pinned_price == pinned
        assert recommendation.current_price == published
        assert fingerprint_of(BUNKERS, STAY, execution) == fingerprint_of(
            BUNKERS, STAY, recommendation
        ), f"{published} published against {pinned} pinned"
