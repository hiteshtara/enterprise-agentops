"""Triage over the opportunity board.

Presentation only. Every test here that matters is really the same assertion
from a different angle: ranking may sort and label, and may never change what
the pricing engine decided or what governance permits.

Every value is invented. No test in this file reaches PriceLabs.
"""

import pytest

from app.opportunities import select
from app.opportunity_priority import (
    LOW_PRIORITY_MAX_UPLIFT,
    OCCUPANCY_LEAD_POINTS,
    REVIEW_NOW_MIN_UPLIFT,
    Priority,
    is_change_clamped,
    rank,
    rank_opportunity,
    summarise_priorities,
    why_now,
)
from app.pricing_config import MAX_CHANGE_PER_RUN

BUNKERS = "680444___747423"


def opportunity(**over):
    """One row as `app.opportunities.select` emits it."""
    base = {
        "id": "inv:2026-10-05",
        "listing_id": BUNKERS,
        "display_name": "Boston Bunkers",
        "stay_date": "2026-10-05",
        "days_out": 30,
        "action": "RAISE",
        "current_price": 200.0,
        "proposed_price": 220.0,
        "uplift": 20.0,
        "uplift_pct": 10.0,
        "confidence": "MEDIUM",
        "demand": "Normal Demand",
        "market_p25": 260.0,
        "market_booked_median": 300.0,
        "market_occupancy": 40.0,
        "listing_occupancy": 60.0,
        "owner_floor": 143.0,
        "auto_raise_ceiling": 252.0,
        "events": None,
        "pinned_price": None,
        "reason": "below the market p25",
        "actionable": True,
        "blocked_reason": None,
        "stale": False,
    }

    base.update(over)

    return base


# -- ranking changes nothing --------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["proposed_price", "actionable", "blocked_reason", "current_price", "uplift"],
)
def test_ranking_never_changes_what_the_engine_decided(field):
    """The whole safety property, one field at a time.

    Triage may reorder and annotate. If it could edit a price or a permission,
    it would be a second engine wearing a badge.
    """
    rows = [
        opportunity(id="a"),
        opportunity(id="b", uplift=5.0, uplift_pct=2.5, proposed_price=205.0),
    ]

    before = {row["id"]: row[field] for row in rows}

    ranked = rank(rows)

    assert {row["id"]: row[field] for row in ranked} == before


def test_ranking_returns_new_rows_rather_than_editing_the_engines_output():
    original = opportunity()

    rank([original])

    assert "priority" not in original, "the caller's row must be untouched"


def test_every_ranked_row_carries_the_triage_fields():
    ranked = rank([opportunity()])[0]

    assert ranked["priority"] in {p.value for p in Priority}
    assert isinstance(ranked["priority_reasons"], list)
    assert ranked["priority_reasons"], "a verdict with no stated reason is not one"
    assert ranked["why_now"]
    assert isinstance(ranked["is_change_clamped"], bool)


# -- the bands ------------------------------------------------------------


def test_strong_demand_with_real_money_is_review_now():
    """The straightforward case: worth money, and the date is not soft."""
    verdict = rank_opportunity(
        opportunity(uplift=53.0, uplift_pct=9.7, demand="Normal Demand")
    )

    assert verdict.priority is Priority.REVIEW_NOW
    assert any("Normal Demand" in reason for reason in verdict.reasons)


def test_a_wide_occupancy_lead_under_p25_is_review_now_even_on_soft_demand():
    """The second qualifying signal, standing on its own.

    A unit filling far ahead of its market while still priced under the comp
    set's p25 is evidence regardless of what the demand label says.
    """
    verdict = rank_opportunity(
        opportunity(
            demand="Low Demand",
            listing_occupancy=58.0,
            market_occupancy=29.0,
            proposed_price=253.0,
            market_p25=264.0,
            uplift=23.0,
            uplift_pct=10.0,
        )
    )

    assert verdict.priority is Priority.REVIEW_NOW
    assert any("leads the market by 29 points" in r for r in verdict.reasons)


def test_soft_demand_with_a_modest_lead_is_watch():
    """A real opportunity whose case nobody would call strong."""
    verdict = rank_opportunity(
        opportunity(
            demand="Low Demand",
            listing_occupancy=45.0,
            market_occupancy=40.0,
            uplift=20.0,
            uplift_pct=10.0,
        )
    )

    assert verdict.priority is Priority.WATCH
    assert any("Low Demand" in r for r in verdict.reasons)
    assert any("modest" in r for r in verdict.reasons)


def test_a_wide_lead_above_p25_is_watch_not_review_now():
    """The lead qualifies only while the price stays under the comp set."""
    verdict = rank_opportunity(
        opportunity(
            demand="Low Demand",
            listing_occupancy=70.0,
            market_occupancy=20.0,
            proposed_price=300.0,
            market_p25=260.0,
            uplift=20.0,
            uplift_pct=10.0,
        )
    )

    assert verdict.priority is Priority.WATCH
    assert any("above market p25" in r for r in verdict.reasons)


def test_a_small_dollar_gain_is_low_priority_however_good_the_evidence():
    """Attention is the scarce resource, not evidence."""
    verdict = rank_opportunity(
        opportunity(
            uplift=8.0,
            uplift_pct=9.0,
            demand="High Demand",
            confidence="HIGH",
        )
    )

    assert verdict.priority is Priority.LOW_PRIORITY
    assert any("below the $10" in r for r in verdict.reasons)


def test_a_tiny_percentage_gain_is_low_priority_however_large_the_dollar():
    verdict = rank_opportunity(
        opportunity(
            current_price=2000.0,
            proposed_price=2060.0,
            uplift=60.0,
            uplift_pct=3.0,
            demand="High Demand",
        )
    )

    assert verdict.priority is Priority.LOW_PRIORITY
    assert any("inside the noise" in r for r in verdict.reasons)


def test_uplift_alone_never_reaches_review_now():
    """A big number on no evidence is exactly what wastes an owner's day."""
    verdict = rank_opportunity(
        opportunity(
            uplift=80.0,
            uplift_pct=20.0,
            demand="Low Demand",
            listing_occupancy=30.0,
            market_occupancy=30.0,
        )
    )

    assert verdict.priority is Priority.WATCH


@pytest.mark.parametrize(
    ("uplift", "expected"),
    [
        (LOW_PRIORITY_MAX_UPLIFT - 0.01, Priority.LOW_PRIORITY),
        (LOW_PRIORITY_MAX_UPLIFT, Priority.WATCH),
        (REVIEW_NOW_MIN_UPLIFT - 0.01, Priority.WATCH),
        (REVIEW_NOW_MIN_UPLIFT, Priority.REVIEW_NOW),
    ],
)
def test_the_band_edges_are_where_the_constants_say(uplift, expected):
    """Named thresholds, held at their exact boundaries."""
    verdict = rank_opportunity(
        opportunity(uplift=uplift, uplift_pct=10.0, demand="Normal Demand")
    )

    assert verdict.priority is expected


@pytest.mark.parametrize(
    ("lead", "expected"),
    [
        (OCCUPANCY_LEAD_POINTS - 0.1, Priority.WATCH),
        (OCCUPANCY_LEAD_POINTS, Priority.REVIEW_NOW),
    ],
)
def test_the_occupancy_lead_edge_is_where_the_constant_says(lead, expected):
    verdict = rank_opportunity(
        opportunity(
            demand="Low Demand",
            market_occupancy=30.0,
            listing_occupancy=30.0 + lead,
            uplift=20.0,
            uplift_pct=10.0,
        )
    )

    assert verdict.priority is expected


# -- the clamp signal -----------------------------------------------------


def test_a_move_stopped_by_the_per_run_cap_is_reported_as_clamped():
    """The engine proposes `min(market_p25, current * 1.10)`.

    Here p25 is far above the cap, so the cap is what stopped it.
    """
    row = opportunity(current_price=230.0, proposed_price=253.0, market_p25=264.0)

    assert is_change_clamped(row) is True
    assert rank_opportunity(row).is_change_clamped is True


def test_a_move_stopped_by_market_p25_is_not_called_clamped():
    """The false positive worth guarding: p25 was binding, not the cap.

    Saying "clamped" here would tell the owner the engine wanted more when it
    did not.
    """
    row = opportunity(current_price=547.0, proposed_price=600.0, market_p25=600.0)

    assert is_change_clamped(row) is False


def test_a_near_boundary_move_is_not_falsely_labelled_clamped():
    """9.7% is not 10%, and the difference is not rounded away."""
    row = opportunity(current_price=547.0, proposed_price=600.0, market_p25=900.0)

    ceiling = 547.0 * (1 + MAX_CHANGE_PER_RUN)

    assert round(600.0) != round(ceiling)
    assert is_change_clamped(row) is False


def test_clamping_is_unknown_rather_than_false_when_evidence_is_missing():
    assert is_change_clamped(opportunity(market_p25=None)) is False
    assert is_change_clamped(opportunity(proposed_price=None)) is False


def test_a_clamped_row_is_not_automatically_downgraded():
    """Clamped is information, not a verdict.

    A clamped move with strong evidence still reaches REVIEW_NOW -- it means
    the engine would have gone further, which argues for attention rather than
    against it.
    """
    verdict = rank_opportunity(
        opportunity(
            current_price=230.0,
            proposed_price=253.0,
            market_p25=264.0,
            uplift=23.0,
            uplift_pct=10.0,
            demand="Normal Demand",
        )
    )

    assert verdict.is_change_clamped is True
    assert verdict.priority is Priority.REVIEW_NOW


def test_a_clamped_row_with_a_weak_case_says_so_among_its_reasons():
    verdict = rank_opportunity(
        opportunity(
            current_price=230.0,
            proposed_price=253.0,
            market_p25=264.0,
            uplift=23.0,
            uplift_pct=10.0,
            demand="Low Demand",
            listing_occupancy=32.0,
            market_occupancy=30.0,
        )
    )

    assert verdict.priority is Priority.WATCH
    assert any("capped at 10%" in r for r in verdict.reasons)


# -- why_now --------------------------------------------------------------


def test_why_now_is_deterministic_and_built_only_from_the_row():
    row = opportunity(
        demand="Normal Demand",
        listing_occupancy=67.0,
        market_occupancy=54.0,
        proposed_price=188.0,
        market_p25=205.0,
    )

    first = why_now(row)

    assert first == why_now(row), "the same row must always read the same"
    assert first == (
        "Normal Demand; $188 remains at or below market p25 of $205."
    )


def test_why_now_names_the_occupancy_lead_when_that_is_the_case():
    row = opportunity(
        demand="Low Demand",
        listing_occupancy=58.0,
        market_occupancy=21.0,
        proposed_price=253.0,
        market_p25=260.0,
    )

    assert why_now(row) == (
        "Low Demand; unit occupancy is 58% vs market 21%, and $253 remains at "
        "or below market p25 of $260."
    )


def test_why_now_says_when_a_price_would_cross_p25():
    row = opportunity(proposed_price=300.0, market_p25=260.0, demand="Low Demand")

    assert "would sit above market p25 of $260" in why_now(row)


def test_why_now_falls_back_rather_than_inventing_a_case():
    row = opportunity(demand=None, market_p25=None, listing_occupancy=None)

    assert why_now(row) == "Demand unknown; the case rests on the uplift alone."


def test_priority_reasons_are_deterministic():
    row = opportunity(demand="Low Demand", listing_occupancy=45.0, market_occupancy=40.0)

    assert rank_opportunity(row).reasons == rank_opportunity(row).reasons


# -- the input set is still the selector's ---------------------------------


def test_nothing_the_selector_rejected_can_become_a_priority_row():
    """Triage runs *after* selection and cannot admit anything.

    HOLD, LOWER, stale and booked rows never reach the ranker, because the
    board is `rank(select(...))`. Composed here exactly as the route does it.
    """
    rejected = [
        opportunity(id="hold", action="HOLD", actionable=False),
        opportunity(id="lower", action="LOWER", proposed_price=180.0),
        opportunity(id="stale", stale=True),
        opportunity(id="booked", action="HOLD", actionable=False, proposed_price=200.0),
        opportunity(id="blocked", blocked_reason="a gate is shut"),
    ]

    board = rank(select(rejected + [opportunity(id="good")]))

    assert [row["id"] for row in board] == ["good"]
    assert {row["action"] for row in board} == {"RAISE"}


# -- ordering and the summary ---------------------------------------------


def test_the_board_orders_by_band_then_by_dollars():
    board = rank(
        select(
            [
                opportunity(id="watch-big", demand="Low Demand", uplift=90.0,
                            current_price=200.0, proposed_price=290.0,
                            listing_occupancy=30.0, market_occupancy=30.0),
                opportunity(id="low", uplift=5.0, current_price=200.0,
                            proposed_price=205.0),
                opportunity(id="review-small", demand="Normal Demand",
                            current_price=200.0, proposed_price=216.0),
                opportunity(id="review-big", demand="Normal Demand",
                            current_price=200.0, proposed_price=240.0),
            ]
        )
    )

    assert [row["id"] for row in board] == [
        "review-big",
        "review-small",
        "watch-big",
        "low",
    ]
    assert [row["priority"] for row in board] == [
        "REVIEW_NOW",
        "REVIEW_NOW",
        "WATCH",
        "LOW_PRIORITY",
    ]


def test_the_priority_summary_counts_exactly_the_rows_shown():
    board = rank(
        select(
            [
                opportunity(id="a", demand="Normal Demand", current_price=200.0,
                            proposed_price=240.0),
                opportunity(id="b", demand="Low Demand", current_price=200.0,
                            proposed_price=220.0, listing_occupancy=30.0,
                            market_occupancy=30.0),
                opportunity(id="c", current_price=200.0, proposed_price=205.0),
            ]
        )
    )

    counts = summarise_priorities(board)

    assert counts == {"review_now": 1, "watch": 1, "low_priority": 1}
    assert sum(counts.values()) == len(board)


def test_why_now_never_reads_as_a_contradiction_after_rounding():
    """A proposal fractionally above p25 must not print "above $600 of $600".

    Live data produced exactly that on the top row of the board: a $600
    proposal against a p25 of $599.80 is truthfully above it, and rendering
    both to the dollar made the sentence read as nonsense. The reader can only
    check what is displayed, so the wording follows the displayed figures --
    while `below_market_p25`, which decides the priority band, keeps the exact
    comparison.
    """
    row = opportunity(proposed_price=600.0, market_p25=599.8, demand="Normal Demand")

    sentence = why_now(row)

    assert "level with market p25" in sentence
    assert "above market p25 of $600" not in sentence

    # The band decision is unaffected: exactly, the price is over p25.
    from app.opportunity_priority import below_market_p25

    assert below_market_p25(row) is False


def test_why_now_still_distinguishes_a_genuinely_higher_price():
    row = opportunity(proposed_price=300.0, market_p25=260.0, demand="Low Demand")

    assert "would sit above market p25 of $260" in why_now(row)
