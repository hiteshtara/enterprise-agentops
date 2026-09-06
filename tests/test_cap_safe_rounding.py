"""Rounding a price to whole dollars must not spend the per-run cap.

`clamp_move` places a proposal exactly on the cap boundary; `round()` then
moves it a few cents further, and on a lower move that is enough for
`check_guardrails` to refuse the whole recommendation. It fires on roughly
half of all prices -- whichever way the cents fall -- which is why exactly one
of eleven live LOWER candidates survived while ten identical-shaped ones did
not.

The fix is in the proposal, not the guardrail. `check_guardrails` is unchanged
and stays the independent verifier: every assertion here that a price is
"valid" is made by asking it, not by re-deriving its arithmetic.

Every value is invented. No test in this file reaches PriceLabs.
"""

import pytest

from app.pricing_config import MAX_CHANGE_PER_RUN, PricingBands
from app.pricing_policy import (
    Confidence,
    PriceAction,
    Refusal,
    cap_safe_price,
    check_guardrails,
)


def bands(**over) -> PricingBands:
    base = {
        "listing_id": "inv-1",
        "slug": "invented",
        "display_name": "Invented Cottage",
        "hard_floor": 1.0,
        "normal_floor": 1.0,
        "auto_raise_ceiling": 10_000.0,
        "absolute_ceiling": 10_000.0,
    }

    base.update(over)

    return PricingBands(**base)


def accepted(action: PriceAction, current: float, proposed: int) -> bool:
    """Whether the *unchanged* guardrail accepts this proposal."""
    return (
        check_guardrails(action, current, float(proposed), bands(), Confidence.HIGH)
        is None
    )


# -- the live failures ----------------------------------------------------


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (228.0, 206),  # boundary 205.20 -- round() gave 205, a 10.09% move
        (217.0, 196),  # boundary 195.30 -- round() gave 195, a 10.14% move
        (229.0, 207),  # boundary 206.10
        (399.0, 360),  # boundary 359.10
    ],
)
def test_a_lower_that_rounded_past_the_floor_now_rounds_onto_it(current, expected):
    """The ten refusals. Each is the largest whole dollar inside the cap."""
    proposed = cap_safe_price(current, current * 0.5)

    assert proposed == expected
    assert accepted(PriceAction.LOWER, current, proposed)

    # ...and the number `round()` produced would still be refused, which is
    # what makes this a fix rather than a coincidence.
    assert not accepted(PriceAction.LOWER, current, expected - 1)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (202.0, 182),  # boundary 181.80 -- round() already gave a valid 182
        (364.0, 328),  # boundary 327.60 -- already valid
    ],
)
def test_a_lower_that_already_rounded_inside_the_cap_is_unchanged(current, expected):
    """The two that worked before must still produce the same price."""
    proposed = cap_safe_price(current, current * 0.5)

    assert proposed == expected
    assert accepted(PriceAction.LOWER, current, proposed)


# -- symmetry -------------------------------------------------------------


def test_a_raise_never_rounds_above_its_ceiling():
    """The same hazard upward: 205.20 becomes 205, not 206."""
    current = 228.0

    proposed = cap_safe_price(current, current * 2)

    assert proposed == 250  # ceiling 250.80
    assert accepted(PriceAction.RAISE, current, proposed)
    assert not accepted(PriceAction.RAISE, current, proposed + 1)


def test_a_raise_that_already_rounded_inside_the_cap_is_unchanged():
    assert cap_safe_price(200.0, 300.0) == 220  # ceiling exactly 220.0


def test_a_proposal_short_of_the_cap_is_simply_rounded():
    """The cap only intervenes at the boundary; ordinary moves round normally."""
    assert cap_safe_price(200.0, 195.4) == 195
    assert cap_safe_price(200.0, 195.6) == 196
    assert cap_safe_price(200.0, 204.4) == 204
    assert cap_safe_price(200.0, 204.6) == 205


def test_a_boundary_that_lands_on_a_whole_dollar_is_taken_exactly():
    """No defensive shaving: 10% of $200 is $20, and $180 is a legal move."""
    proposed = cap_safe_price(200.0, 100.0)

    assert proposed == 180
    assert accepted(PriceAction.LOWER, 200.0, proposed)

    up = cap_safe_price(200.0, 400.0)

    assert up == 220
    assert accepted(PriceAction.RAISE, 200.0, up)


@pytest.mark.parametrize("current", [11.0, 19.0, 23.0, 47.0])
def test_small_prices_stay_inside_the_cap(current):
    """Where a dollar is a large fraction of the price, rounding matters most."""
    for target in (current * 0.5, current * 2):
        proposed = cap_safe_price(current, target)

        assert abs(proposed - current) / current <= MAX_CHANGE_PER_RUN + 1e-9


# -- the invariant, over a range ------------------------------------------


@pytest.mark.parametrize("cents", [0, 10, 25, 33, 50, 67, 75, 90, 99])
def test_the_cap_holds_across_every_price_and_both_directions(cents):
    """The property, not a handful of examples.

    A fix that worked at $228 and nowhere else would be no fix, and the
    failure mode is entirely about where the cents fall -- so the sweep varies
    exactly that, across the whole plausible nightly range.
    """
    for whole in range(20, 1200, 7):
        current = whole + cents / 100

        down = cap_safe_price(current, current * 0.1)
        up = cap_safe_price(current, current * 5)

        assert abs(down - current) / current <= MAX_CHANGE_PER_RUN + 1e-9, (
            f"${current} lowered to ${down}"
        )
        assert abs(up - current) / current <= MAX_CHANGE_PER_RUN + 1e-9, (
            f"${current} raised to ${up}"
        )

        # Asked of the guardrail itself, not re-derived from the arithmetic.
        assert accepted(PriceAction.LOWER, current, down), f"${current} -> ${down}"
        assert accepted(PriceAction.RAISE, current, up), f"${current} -> ${up}"


def test_the_guardrail_still_refuses_a_genuinely_oversized_move():
    """The cap was made expressible, not softer.

    A tolerance inside `check_guardrails` would have fixed the rounding and
    let a real 11% move through by the same margin. This holds that it did
    not.
    """
    assert (
        check_guardrails(
            PriceAction.LOWER,
            228.0,
            200.0,  # 12.3%, nothing to do with rounding
            bands(),
            Confidence.HIGH,
        )
        is Refusal.EXCEEDS_MAX_CHANGE
    )


def test_rounding_never_reaches_past_the_cap_to_a_floor_or_ceiling():
    """`cap_safe_price` bounds the *move*; the bands bound the *price*.

    They are separate checks and this one must not be mistaken for the other:
    a cap-safe step can still land under a hard floor, and it is
    `check_guardrails` that refuses it.
    """
    proposed = cap_safe_price(200.0, 100.0)

    assert proposed == 180

    assert (
        check_guardrails(
            PriceAction.LOWER,
            200.0,
            float(proposed),
            bands(hard_floor=190.0),
            Confidence.HIGH,
        )
        is Refusal.BELOW_HARD_FLOOR
    )
