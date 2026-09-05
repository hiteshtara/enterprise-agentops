"""The owner dynamic floor: schedule resolution and its safety properties.

The floors themselves were derived from 12 months of the owner's own booking
history and backtested before being approved; that derivation is recorded in
`app/pricing_config.py`. What is tested here is the machinery around them --
that a schedule resolves to the right band, that it can never dip under the
hard floor, that a property without a schedule falls back honestly instead of
inventing one, and that the two floors stay distinct kinds of thing.

Every listing id below is a real one, because the schedules *are* the subject.
No test here reaches PriceLabs.
"""

import datetime

import pytest

from app.pricing_config import (
    BANDS,
    FloorBand,
    PricingBands,
    bands_for,
    dynamic_floor,
)

SECOND_FLOOR = "680434___747413"
ARBORETUM = "681301___748348"
THIRD_FLOOR = "680420___747399"
BUNKERS = "680444___747423"
MODERN_CONDO = "680447___747426"
HARVARD = "681286___748333"
BOSTON_CONDO_SECOND_FLOOR = "681293___748340"

#: An ordinary summer night, so the seasonal bands do not confound the
#: lead-time ones.
SUMMER = datetime.date(2026, 7, 15)


def floor_at(listing_id: str, stay_date: datetime.date, days_out: int):
    return dynamic_floor(bands_for(listing_id), stay_date, days_out)


@pytest.mark.parametrize(
    ("days_out", "expected"),
    [
        (120, 240.0),
        (30, 240.0),
        (29, 175.0),
        (15, 175.0),
        (14, 170.0),
        (8, 170.0),
        (7, 170.0),
        (4, 170.0),
        (3, 170.0),
        (0, 170.0),
    ],
)
def test_the_second_floor_schedule_declines_toward_arrival(days_out, expected):
    """The owner-approved table, band edges included.

    The edges are the point: 30 and 29 sit either side of a $65 step, and an
    off-by-one there would silently price two weeks of inventory wrong.
    """
    resolved = floor_at(SECOND_FLOOR, SUMMER, days_out)

    assert resolved is not None
    assert resolved[0] == expected


def test_the_second_floor_floor_far_out_exceeds_the_rejected_flat_floor():
    """Dynamic is not a euphemism for lower.

    The flat $215 this replaced would have *undercut* the property 30+ days
    out, where it actually converts at a $355 median. A schedule that only ever
    moved downward would have missed that entirely.
    """
    far, near = floor_at(SECOND_FLOOR, SUMMER, 45), floor_at(SECOND_FLOOR, SUMMER, 2)

    assert far is not None and near is not None
    assert far[0] > 215.0 > near[0]


def test_arboretum_is_seasonal_and_carries_no_lead_time_bands():
    """Winter and summer differ; lead time within a season does not.

    Adding lead-time bands here backtested worse (9 refusals against 5), so
    their absence is a finding rather than an omission, and a change that
    introduced them would be reverting a decision.
    """
    for days_out in (60, 20, 5, 0):
        winter = floor_at(ARBORETUM, datetime.date(2027, 2, 10), days_out)
        summer = floor_at(ARBORETUM, SUMMER, days_out)

        assert winter is not None and summer is not None
        assert winter[0] == 189.0
        assert summer[0] == 205.0


def test_march_is_winter_and_april_is_not():
    """The season boundary the booking history actually drew."""
    march = floor_at(ARBORETUM, datetime.date(2027, 3, 31), 40)
    april = floor_at(ARBORETUM, datetime.date(2027, 4, 1), 40)

    assert march is not None and april is not None
    assert march[0] == 189.0
    assert april[0] == 205.0


@pytest.mark.parametrize("listing_id", [THIRD_FLOOR, BUNKERS])
def test_the_roslindale_pair_are_flat_at_their_hard_floor(listing_id):
    """Held at MIN $143 on their own evidence, in every band and season."""
    for stay_date in (SUMMER, datetime.date(2027, 1, 20)):
        for days_out in (90, 10, 0):
            resolved = floor_at(listing_id, stay_date, days_out)

            assert resolved is not None
            assert resolved[0] == 143.0


@pytest.mark.parametrize(
    "listing_id",
    [MODERN_CONDO, HARVARD, BOSTON_CONDO_SECOND_FLOOR],
)
def test_a_property_without_a_schedule_returns_none_rather_than_a_guess(listing_id):
    """Three of the seven have not been analysed. Absence stays absence.

    Named individually and checked across the whole lead-time range, because
    the failure this guards against is not a typo -- it is someone adding a
    generic portfolio-wide default so that every property "has" a floor. That
    would undo the entire property-specific approach in one edit while every
    other test here still passed, since a default is indistinguishable from a
    derived floor once it is in the table.

    The right way to give one of these three a schedule is to derive it from
    that property's own booking history and backtest it, as was done for the
    2nd-Floor Home and Arboretum -- and then to delete it from this list.
    """
    for days_out in (120, 30, 29, 15, 14, 7, 3, 0):
        assert floor_at(listing_id, SUMMER, days_out) is None
        assert floor_at(listing_id, datetime.date(2027, 2, 10), days_out) is None


def test_a_schedule_can_never_resolve_below_the_hard_floor():
    """The dynamic floor may raise the line. It may never lower it.

    Guarded here rather than trusted to review, because the two floors are
    edited independently: someone lowering one band by $20 must not be able to
    quietly take a property under its safety line.
    """
    band = PricingBands(
        listing_id="inv-1",
        slug="invented",
        display_name="Invented Cottage",
        hard_floor=200.0,
        normal_floor=240.0,
        auto_raise_ceiling=400.0,
        absolute_ceiling=500.0,
        floor_schedule=(FloorBand(floor=120.0, basis="deliberately too low"),),
    )

    resolved = dynamic_floor(band, SUMMER, 30)

    assert resolved is not None
    assert resolved[0] == 200.0
    assert "hard floor" in resolved[1]


def test_every_band_carries_its_evidence():
    """A floor without a basis is a number nobody can re-derive."""
    for band in BANDS:
        for rule in band.floor_schedule:
            assert rule.basis.strip()


def test_no_schedule_sits_under_its_own_hard_floor():
    """The approved table, checked against the approved safety lines."""
    for band in BANDS:
        for rule in band.floor_schedule:
            assert rule.floor >= band.hard_floor, band.slug


def test_pricelabs_minimums_are_not_moved_by_a_dynamic_floor():
    """The two are different things, and this is the one that must not drift.

    The owner's instruction was explicit: keep the PriceLabs permanent MINs as
    they are and let AgentGuard hold its own view. 2nd-Floor $170 and
    Arboretum $189 are those MINs; a future edit that pushed them to the
    backtested $215/$220 would be the flat-floor approach coming back.
    """
    assert bands_for(SECOND_FLOOR).hard_floor == 170.0
    assert bands_for(ARBORETUM).hard_floor == 189.0
