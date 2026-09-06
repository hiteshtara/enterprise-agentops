"""Owner authorization for LOWER, and what it deliberately does not do.

The distinction this file exists to hold: `BOOKING_COM_DISCOUNT_EXPOSURE_
VERIFIED` is a statement about what has been *measured*, and stays False
because nothing has been. `OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_
EXPOSURE` is a statement about what the owner has *decided to do anyway*.

Setting the verification flag True would have opened LOWER in one line and
told every future reader the exposure had been established. Two flags keep the
fact and the decision apart.

Every value is invented. No test in this file reaches PriceLabs.
"""

import datetime

import pytest

import app.pricing_config as config
from app.lower_priority import (
    CONFIDENT_SAMPLE_COUNT,
    MATERIAL_GAP_PCT,
    REVIEW_NOW_DAYS_OUT,
    LowerFlag,
    rank,
    rank_lower,
    select_lower,
    summarise_lower,
)
from app.opportunity_priority import Priority
from app.pricing_config import (
    BANDS,
    BOOKING_COM_UNCERTAINTY_WARNING,
    MAX_CHANGE_PER_RUN,
    observed_commission_rate,
    unverified_reason,
)

BUNKERS = "680444___747423"
CONDO_2F = "681293___748340"
ARBORETUM = "681301___748348"


def row(**over):
    """A LOWER payload as `to_payload` emits it."""
    base = {
        "id": f"{BUNKERS}:2026-09-11",
        "listing_id": BUNKERS,
        "slug": "boston-bunkers",
        "display_name": "Boston Bunkers",
        "stay_date": "2026-09-11",
        "days_out": 5,
        "action": "LOWER",
        "current_price": 201.0,
        "proposed_price": 181.0,
        "confidence": "MEDIUM",
        "demand": "Normal Demand",
        "market_p25": 206.0,
        "market_occupancy": 57.4,
        "listing_occupancy": 65.0,
        "hard_floor": 143.0,
        "owner_floor": 143.0,
        "historical_lead_band_adr": 149.5,
        "history_sample_count": 9,
        "historical_reference_gap_dollars": 51.5,
        "historical_reference_gap_pct": 25.6,
        "below_owner_floor": False,
        "market_signal_conflict": False,
        "booking_com_warning": BOOKING_COM_UNCERTAINTY_WARNING,
        "observed_commission_rate": 23.0,
        "actionable": True,
        "blocked_reason": None,
        "stale": False,
        "reason": "still open; asking above what this property converts at",
    }

    base.update(over)

    return base


# -- verification and authorization are different things -------------------


def test_the_verification_flag_is_still_false():
    """Nothing about the Booking.com exposure has been measured.

    If this ever reads True, it must be because someone established the
    maximum effective guest discount -- not because LOWER needed opening.
    """
    assert config.BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED is False


def test_owner_authorization_alone_releases_lower_from_the_channel_gate():
    assert config.OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE is True

    for band in BANDS:
        assert unverified_reason("LOWER", band.listing_id) is None, band.slug


def test_withdrawing_the_authorization_re_blocks_lower_immediately(monkeypatch):
    """The decision is reversible, and reversing it is a one-line change."""
    monkeypatch.setattr(
        config, "OWNER_AUTHORIZES_LOWER_WITH_UNVERIFIED_BOOKING_EXPOSURE", False
    )

    for band in BANDS:
        blocked = unverified_reason("LOWER", band.listing_id)

        assert blocked is not None, band.slug
        assert "Booking.com" in blocked


def test_the_authorization_does_not_touch_raise_or_remove_pin():
    for band in BANDS:
        assert unverified_reason("RAISE", band.listing_id) is None
        assert unverified_reason("REMOVE_PIN", band.listing_id) is None


def test_the_other_verification_flags_are_unchanged():
    assert config.CLEANUP_STRATEGY_VERIFIED is True
    assert config.EXPIRY_SEMANTICS_VERIFIED is False
    assert config.ONE_NIGHT_STAYS_ALLOWED is False


# -- the warning -----------------------------------------------------------


def test_every_lower_carries_the_uncertainty_warning():
    from app.pricing_config import bands_for
    from app.pricing_policy import (
        Confidence,
        MarketState,
        PriceAction,
        Recommendation,
        to_payload,
    )

    band = bands_for(BUNKERS)

    payload = to_payload(
        Recommendation(
            listing_id=BUNKERS,
            slug=band.slug,
            display_name=band.display_name,
            stay_date=datetime.date(2026, 9, 11),
            days_out=5,
            action=PriceAction.LOWER,
            current_price=201.0,
            proposed_price=181.0,
            confidence=Confidence.MEDIUM,
            reason="test",
            state=MarketState(
                current_price=201.0,
                market_p25=206.0,
                market_booked_median=260.0,
                market_occupancy=57.4,
                listing_occupancy=65.0,
                demand="Normal Demand",
                pickup_7_days=None,
                pinned_price=None,
                last_refreshed_at=None,
            ),
            bands=band,
            history_adr=149.5,
            history_count=9,
        )
    )

    assert payload["booking_com_warning"] == BOOKING_COM_UNCERTAINTY_WARNING
    assert "not fully verified" in payload["booking_com_warning"]
    assert "maximum effective guest discount is unknown" in payload["booking_com_warning"]


def test_the_warning_never_claims_the_exposure_is_verified():
    text = BOOKING_COM_UNCERTAINTY_WARNING.lower()

    assert "not fully verified" in text
    assert "unknown" in text
    assert "is verified" not in text
    assert "has been verified" not in text


# -- observed commission, not contractual ----------------------------------


#: The owner's own grouping. Not inferable from listing names -- an earlier
#: draft guessed from wording and put Modern Condo under Roslindale and left
#: Arboretum unmapped, both wrong.
EXPECTED_GROUPS = {
    "roslindale-3rd-floor": ("roslindale", 23.0),
    "renovated-2nd-floor": ("roslindale", 23.0),
    "boston-bunkers": ("roslindale", 23.0),
    "arboretum": ("roslindale", 23.0),
    "modern-condo": ("jp-forest-hill", 18.0),
    "boston-condo-second-floor": ("jp-forest-hill", 18.0),
    "harvard": ("allston", 18.0),
}


def test_every_listing_maps_to_its_invoice_group_and_observed_rate():
    """All seven, by slug, against the rate its group's invoice actually billed."""
    assert set(config.LISTING_INVOICE_GROUP) == set(EXPECTED_GROUPS)

    for band in BANDS:
        group, rate = EXPECTED_GROUPS[band.slug]

        assert config.LISTING_INVOICE_GROUP[band.slug] == group, band.slug
        assert observed_commission_rate(band.listing_id) == rate, band.slug


def test_the_roslindale_four_share_one_invoice_rate():
    roslindale = [s for s, (g, _) in EXPECTED_GROUPS.items() if g == "roslindale"]

    assert len(roslindale) == 4
    assert "arboretum" in roslindale, "Arboretum is billed under Roslindale"
    assert "modern-condo" not in roslindale, "Modern Condo is JP / Forest Hill"


def test_an_unknown_listing_has_no_rate():
    """No portfolio-wide fallback: absence stays absence."""
    assert observed_commission_rate("not-a-listing") is None
    assert observed_commission_rate(None) is None
    assert observed_commission_rate("") is None


def test_there_is_no_default_commission_rate():
    """A lookup miss must not resolve to any group's rate.

    Guarded because a `.get(group, SOMETHING)` added later would silently
    attach a billed rate to an account nobody has an invoice for.
    """
    import inspect

    source = inspect.getsource(config.observed_commission_rate)

    assert "OBSERVED_COMMISSION_RATES.get(group)" in source
    assert "OBSERVED_COMMISSION_RATES.get(group," not in source
    assert config.OBSERVED_COMMISSION_RATES.get("no-such-group") is None


def test_observed_rates_are_recorded_as_observations():
    """Three invoice groups, each read off an actual August 2026 statement."""
    assert config.OBSERVED_COMMISSION_RATES == {
        "roslindale": 23.0,
        "jp-forest-hill": 18.0,
        "allston": 18.0,
    }


# -- no maximum is derived -------------------------------------------------


def test_the_observed_discount_combinations_are_kept_as_text():
    """Evidence, deliberately not arithmetic.

    Keeping them as strings is what stops anyone combining them: there is no
    number here to multiply, and the stacking rule is unknown.
    """
    assert len(config.OBSERVED_DISCOUNT_COMBINATIONS) == 4

    for entry in config.OBSERVED_DISCOUNT_COMBINATIONS:
        assert isinstance(entry, str)
        assert "Genius Dynamic" in entry


def test_no_maximum_channel_discount_exists_anywhere():
    """The name itself must not appear. A ceiling nobody measured is a guess."""
    import pathlib

    for path in pathlib.Path("app").rglob("*.py"):
        source = path.read_text()

        # An assignment, not a mention: the config docstring names both in
        # prose precisely to record that they do not exist.
        assert "MAX_CHANNEL_DISCOUNT =" not in source, path
        assert "MAX_CHANNEL_DISCOUNT:" not in source, path
        assert "OWNER_NET_FLOOR =" not in source, path
        assert "OWNER_NET_FLOOR:" not in source, path


def test_no_floor_is_divided_by_a_discount():
    """`OWNER_NET_FLOOR / (1 - discount)` was explicitly ruled out."""
    import pathlib
    import re

    pattern = re.compile(r"/\s*\(\s*1\s*-\s*\w*discount", re.IGNORECASE)

    for path in pathlib.Path("app").rglob("*.py"):
        assert not pattern.search(path.read_text()), path


# -- LOWER priority --------------------------------------------------------


def test_a_close_material_well_evidenced_reduction_is_review_now():
    verdict = rank_lower(row(days_out=3, historical_reference_gap_pct=26.0,
                             history_sample_count=9))

    assert verdict.priority is Priority.REVIEW_NOW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("days_out", REVIEW_NOW_DAYS_OUT + 1),
        ("historical_reference_gap_pct", MATERIAL_GAP_PCT - 0.1),
        ("history_sample_count", CONFIDENT_SAMPLE_COUNT - 1),
        ("below_owner_floor", True),
        ("market_signal_conflict", True),
    ],
)
def test_each_missing_condition_drops_it_to_watch(field, value):
    """WATCH is the default, not the leftovers. Any one gap is enough."""
    base = {"days_out": 3, "historical_reference_gap_pct": 26.0,
            "history_sample_count": 9}

    base[field] = value

    verdict = rank_lower(row(**base))

    assert verdict.priority is Priority.WATCH


def test_a_below_owner_floor_reduction_is_flagged_and_explained():
    verdict = rank_lower(row(below_owner_floor=True))

    assert LowerFlag.BELOW_OWNER_FLOOR.value in verdict.reasons[0] or any(
        "below the owner floor" in r for r in verdict.reasons
    )
    assert "explicit vacancy decision" in verdict.why_now


def test_the_board_orders_by_proximity_to_arrival_not_dollars():
    """The night that runs out of time first is the one to look at first."""
    board = rank(
        [
            row(id="far", days_out=12, stay_date="2026-09-20",
                current_price=400.0, proposed_price=360.0),
            row(id="near", days_out=2, stay_date="2026-09-08",
                current_price=200.0, proposed_price=181.0),
        ]
    )

    assert [r["id"] for r in board] == ["near", "far"]


def test_the_summary_counts_exactly_what_is_shown():
    board = rank(
        [
            row(id="a", days_out=3),
            row(id="b", days_out=4, below_owner_floor=True),
            row(id="c", days_out=5, market_signal_conflict=True),
        ]
    )

    summary = summarise_lower(board)

    assert summary["opportunities"] == 3
    assert summary["review_now"] + summary["watch"] == 3
    assert summary["below_owner_floor"] == 1
    assert summary["market_signal_conflict"] == 1
    assert summary["total_reduction"] == pytest.approx(
        sum(r["current_price"] - r["proposed_price"] for r in board)
    )


def test_the_summary_never_calls_a_reduction_revenue():
    """These nights are unsold; there is no revenue to lose."""
    keys = set(summarise_lower(rank([row()])))

    assert "lost_revenue" not in keys
    assert "expected_revenue" not in keys
    assert "total_reduction" in keys


# -- selection defers to the engine ----------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"action": "RAISE"},
        {"action": "HOLD"},
        {"actionable": False},
        {"blocked_reason": "a gate is shut"},
        {"stale": True},
        {"proposed_price": None},
    ],
)
def test_selection_excludes_anything_the_engine_did_not_release(over):
    assert select_lower([row(**over)]) == []


def test_a_released_lower_is_selected():
    assert len(select_lower([row()])) == 1


# -- the guardrails are untouched ------------------------------------------


def test_the_per_run_cap_is_unchanged():
    assert MAX_CHANGE_PER_RUN == 0.10


def test_authorization_cannot_carry_a_price_under_the_hard_floor(monkeypatch):
    """No human approval, and no owner policy, overrides the hard floor."""
    from app.pricing_config import bands_for
    from app.pricing_policy import Confidence, PriceAction, Refusal, check_guardrails

    band = bands_for(BUNKERS)

    refusal = check_guardrails(
        PriceAction.LOWER,
        band.hard_floor + 5,
        band.hard_floor - 1,
        band,
        Confidence.HIGH,
    )

    assert refusal is Refusal.BELOW_HARD_FLOOR


def test_one_night_stays_remain_prohibited():
    assert config.ONE_NIGHT_STAYS_ALLOWED is False
