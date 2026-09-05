"""Vacancy board analysis.

Every calendar here is invented. No test in this file talks to PriceLabs, and
none of the numbers came from a real portfolio.
"""

from datetime import date, timedelta

import pytest

from app.connectors.pricelabs.models import (
    ListingHealth,
    Night,
    NightState,
    PropertyCalendar,
)
from app.connectors.pricelabs.normalise import (
    listing_health,
    night_state,
    property_calendar,
)
from app.vacancy import (
    ALLOWED_HORIZONS,
    HIGH_VALUE_PERCENTILE,
    MIN_PRICED_NIGHTS_FOR_PERCENTILE,
    build_board,
    high_value_threshold,
    percentile,
    runs_of,
)

# A Monday, so weekday offsets are unambiguous: offset 4 is Friday, 5 Saturday.
START = date(2026, 9, 7)


def night(
    offset: int,
    state: NightState,
    price: float | None = 200.0,
    minimum_stay: int | None = 3,
) -> Night:
    return Night(
        stay_date=START + timedelta(days=offset),
        state=state,
        price=price,
        minimum_stay=minimum_stay,
    )


def calendar(
    nights: list[Night],
    listing_id: str = "inv-1",
    name: str = "Invented Cottage",
    health: ListingHealth | None = None,
    refreshed: str | None = "2026-09-07T09:15:00+00:00",
) -> PropertyCalendar:
    return PropertyCalendar(
        listing_id=listing_id,
        display_name=name,
        nights=tuple(nights),
        last_refreshed_at=refreshed,
        health=health,
    )


def board(calendars: list[PropertyCalendar], horizon: int = 30) -> dict:
    return build_board(
        calendars,
        horizon_days=horizon,
        horizon_start=START,
        source_name="Fixtures",
        source_is_live=False,
    )


# -- night classification --------------------------------------------------


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"booking_status": "Booked", "occupancy": 1}, NightState.BOOKED),
        (
            {"booking_status": "Booked (Check-In)", "occupancy": 1},
            NightState.BOOKED,
        ),
        (
            {"booking_status": "", "occupancy": 0, "unbookable": 0},
            NightState.OPEN,
        ),
        (
            {"booking_status": "", "occupancy": 0, "unbookable": 1},
            NightState.UNBOOKABLE,
        ),
        (
            {"booking_status": "Blocked", "occupancy": 0, "unbookable": 0},
            NightState.BLOCKED,
        ),
    ],
)
def test_night_states_are_classified_from_provider_fields(row, expected):
    assert night_state(row) is expected


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"occupancy": 0},
        {"booking_status": ""},
        {"booking_status": "", "occupancy": 0},
        {"booking_status": "", "occupancy": 0, "unbookable": None},
        {"booking_status": "Awaiting sync", "occupancy": 0, "unbookable": 0},
        {"booking_status": None, "occupancy": 0, "unbookable": 0},
    ],
)
def test_unknown_never_becomes_open(row):
    """The invariant this whole board rests on.

    An unestablished night is not inventory we may sell. Every ambiguous shape
    must land on UNKNOWN -- never OPEN.
    """
    assert night_state(row) is NightState.UNKNOWN


def test_unknown_nights_are_excluded_from_sellable_totals():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.OPEN, 100.0),
                    night(1, NightState.UNKNOWN, 100.0),
                ]
            )
        ]
    )

    assert result["summary"]["open_sellable_nights"] == 1
    assert result["summary"]["unknown_nights"] == 1
    assert result["summary"]["sellable_gross_value"] == 100.0


def test_blocked_is_not_reported_as_unbookable():
    """Owner-blocked and restriction-unbookable are different problems.

    Folding blocked nights into the unbookable queue would recommend relaxing a
    minimum stay for a night the owner deliberately closed.
    """
    result = board(
        [
            calendar(
                [
                    night(0, NightState.BLOCKED, 300.0),
                    night(1, NightState.UNBOOKABLE, 300.0),
                ]
            )
        ]
    )

    assert result["summary"]["blocked_nights"] == 1
    assert result["summary"]["unbookable_nights"] == 1
    assert len(result["unbookable_windows"]) == 1


# -- windows ---------------------------------------------------------------


def test_consecutive_open_nights_form_one_window():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.OPEN, 100.0),
                    night(1, NightState.OPEN, 100.0),
                    night(2, NightState.OPEN, 100.0),
                    night(3, NightState.BOOKED, 100.0),
                    night(4, NightState.OPEN, 100.0),
                ]
            )
        ]
    )

    windows = result["open_windows"]

    assert [w["nights"] for w in windows] == [3, 1]
    assert windows[0]["start"] == "2026-09-07"
    assert windows[0]["end"] == "2026-09-09"


def test_a_gap_in_the_calendar_does_not_join_two_windows():
    """Nights either side of a night the source never returned are not adjacent."""
    nights = [
        night(0, NightState.OPEN, 100.0),
        night(2, NightState.OPEN, 100.0),
    ]

    assert [len(run) for run in runs_of(nights, NightState.OPEN)] == [1, 1]


def test_weekend_nights_are_counted_as_friday_and_saturday():
    result = board(
        [calendar([night(offset, NightState.OPEN, 100.0) for offset in range(7)])]
    )

    window = result["open_windows"][0]

    assert window["nights"] == 7
    assert window["weekend_nights"] == 2


def test_gross_value_and_adr_come_from_the_nightly_prices():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.OPEN, 100.0),
                    night(1, NightState.OPEN, 300.0),
                ]
            )
        ]
    )

    window = result["open_windows"][0]

    assert window["gross_value"] == 400.0
    assert window["adr"] == 200.0


def test_a_missing_price_is_counted_but_adds_no_revenue():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.OPEN, 100.0),
                    night(1, NightState.OPEN, None),
                ]
            )
        ]
    )

    window = result["open_windows"][0]

    assert window["nights"] == 2
    assert window["priced_nights"] == 1
    assert window["gross_value"] == 100.0
    assert window["adr"] == 100.0
    assert window["complete_pricing"] is False


def test_a_window_with_no_prices_reports_no_value_rather_than_zero():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.OPEN, None),
                    night(1, NightState.OPEN, None),
                ]
            )
        ]
    )

    window = result["open_windows"][0]

    assert window["gross_value"] is None
    assert window["adr"] is None
    # An unpriceable window cannot be ranked against priced ones.
    assert result["opportunities"] == []


# -- orphans ---------------------------------------------------------------


def test_one_night_and_two_night_orphans_are_classified():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.BOOKED, 100.0),
                    night(1, NightState.UNBOOKABLE, 150.0),
                    night(2, NightState.BOOKED, 100.0),
                    night(3, NightState.UNBOOKABLE, 150.0),
                    night(4, NightState.UNBOOKABLE, 150.0),
                    night(5, NightState.BOOKED, 100.0),
                ]
            )
        ]
    )

    classes = sorted(w["orphan_class"] for w in result["unbookable_windows"])

    assert classes == ["one_night", "two_night"]
    assert result["summary"]["unbookable_gross_value"] == 450.0


def test_orphan_reason_is_only_stated_when_the_data_establishes_it():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.UNBOOKABLE, 150.0, minimum_stay=3),
                    night(1, NightState.BOOKED, 100.0),
                    night(2, NightState.UNBOOKABLE, 150.0, minimum_stay=None),
                ]
            )
        ]
    )

    reasons = {w["start"]: w["reason"] for w in result["unbookable_windows"]}

    assert reasons["2026-09-07"] == "3-night minimum against a 1-night gap"
    # No minimum stay came back for this one, so nothing is asserted about it.
    assert reasons["2026-09-09"] is None


def test_a_high_value_orphan_is_flagged():
    nights = [night(offset, NightState.BOOKED, 100.0) for offset in range(12)]

    nights.append(night(12, NightState.UNBOOKABLE, 900.0))

    result = board([calendar(nights)])

    orphan = result["unbookable_windows"][0]

    assert orphan["high_value"] is True
    assert orphan["high_value_nights"] == 1


def test_unbookable_nights_are_never_added_to_sellable_totals():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.OPEN, 100.0),
                    night(1, NightState.UNBOOKABLE, 900.0),
                ]
            )
        ]
    )

    assert result["summary"]["open_sellable_nights"] == 1
    assert result["summary"]["sellable_gross_value"] == 100.0
    assert result["summary"]["unbookable_gross_value"] == 900.0


# -- high value ------------------------------------------------------------


def test_percentile_is_linear_interpolation():
    assert percentile([1, 2, 3, 4], 75.0) == 3.25
    assert percentile([10], 75.0) == 10
    assert percentile([], 75.0) is None


def test_high_value_is_relative_to_each_property_not_the_portfolio():
    """A cheap property's best night beats an expensive property's worst.

    One portfolio-wide dollar threshold would hide every opportunity in the
    cheaper property and flag routine nights in the dearer one.
    """
    cheap = calendar(
        [night(offset, NightState.BOOKED, 100.0) for offset in range(10)]
        + [night(10, NightState.OPEN, 180.0)],
        listing_id="cheap",
        name="Cheap",
    )

    dear = calendar(
        [night(offset, NightState.BOOKED, 900.0) for offset in range(10)]
        + [night(10, NightState.OPEN, 500.0)],
        listing_id="dear",
        name="Dear",
    )

    result = board([cheap, dear])

    flagged = {entry["listing_id"] for entry in result["high_value_nights"]}

    assert flagged == {"cheap"}


def test_high_value_needs_enough_priced_nights_to_be_meaningful():
    few = [night(offset, NightState.OPEN, 100.0 * (offset + 1)) for offset in range(3)]

    assert len(few) < MIN_PRICED_NIGHTS_FOR_PERCENTILE
    assert high_value_threshold(calendar(few)) is None
    assert board([calendar(few)])["high_value_nights"] == []


def test_high_value_threshold_is_the_documented_percentile():
    nights = [
        night(offset, NightState.BOOKED, float(100 * (offset + 1)))
        for offset in range(10)
    ]

    prices = [float(100 * (offset + 1)) for offset in range(10)]

    assert high_value_threshold(calendar(nights)) == percentile(
        prices,
        HIGH_VALUE_PERCENTILE,
    )


def test_high_value_nights_report_distance_above_the_property_median():
    nights = [night(offset, NightState.BOOKED, 100.0) for offset in range(10)]

    nights.append(night(10, NightState.OPEN, 150.0))

    entry = board([calendar(nights)])["high_value_nights"][0]

    assert entry["pct_above_median"] == 50.0


# -- occupancy -------------------------------------------------------------


def test_occupancy_is_booked_over_nights_of_known_state():
    result = board(
        [
            calendar(
                [
                    night(0, NightState.BOOKED, 100.0),
                    night(1, NightState.BOOKED, 100.0),
                    night(2, NightState.OPEN, 100.0),
                    night(3, NightState.BLOCKED, 100.0),
                ]
            )
        ]
    )

    assert result["summary"]["occupancy_pct"] == 50.0


def test_unknown_nights_leave_the_occupancy_denominator():
    """We do not know that night's state, so it cannot count either way."""
    result = board(
        [
            calendar(
                [
                    night(0, NightState.BOOKED, 100.0),
                    night(1, NightState.OPEN, 100.0),
                    night(2, NightState.UNKNOWN, None),
                ]
            )
        ]
    )

    assert result["summary"]["occupancy_pct"] == 50.0


def test_occupancy_is_unknown_rather_than_zero_when_nothing_is_known():
    result = board([calendar([night(0, NightState.UNKNOWN, None)])])

    assert result["summary"]["occupancy_pct"] is None


# -- needs attention -------------------------------------------------------


def below_market_health() -> ListingHealth:
    return ListingHealth(
        month_label="October(High Season)",
        market_occupancy_pct=35.0,
        listing_occupancy_pct=19.0,
        booking_window_min_days=4,
        booking_window_max_days=47,
        provider_flag="Your listing is outperforming the market.",
    )


def test_a_property_materially_below_its_market_needs_attention():
    result = board(
        [
            calendar(
                [night(0, NightState.BOOKED, 100.0)],
                health=below_market_health(),
            )
        ]
    )

    assert len(result["needs_attention"]) == 1
    assert result["needs_attention"][0]["occupancy_gap_points"] == 16.0
    assert "19% against a market at 35%" in result["needs_attention"][0]["reasons"][0]


def test_a_small_occupancy_gap_is_not_flagged():
    health = ListingHealth(
        month_label="October",
        market_occupancy_pct=35.0,
        listing_occupancy_pct=31.0,
    )

    result = board([calendar([night(0, NightState.BOOKED, 100.0)], health=health)])

    assert result["needs_attention"] == []


def test_provider_recommendations_are_carried_through_verbatim():
    """The only pricing advice this board shows is PriceLabs' own."""
    health = ListingHealth(
        provider_recommendations=("Adjust your Minimum Price",),
    )

    result = board([calendar([night(0, NightState.OPEN, 100.0)], health=health)])

    assert result["needs_attention"][0]["reasons"] == [
        "PriceLabs recommends: Adjust your Minimum Price",
    ]


# -- ranking ---------------------------------------------------------------


def test_opportunities_rank_by_value_and_explain_themselves():
    small = calendar(
        [night(offset, NightState.OPEN, 100.0) for offset in range(2)],
        listing_id="small",
        name="Small",
    )

    large = calendar(
        [night(offset, NightState.OPEN, 400.0) for offset in range(4)],
        listing_id="large",
        name="Large",
    )

    ranked = board([small, large])["opportunities"]

    assert [entry["listing_id"] for entry in ranked] == ["large", "small"]
    assert ranked[0]["rank"] == 1
    assert "$1,600 open value" in ranked[0]["reasons"]
    assert ranked[0]["score"] >= ranked[1]["score"]


def test_ranking_is_deterministic_across_input_order():
    first = calendar(
        [night(offset, NightState.OPEN, 300.0) for offset in range(3)],
        listing_id="a",
        name="A",
    )

    second = calendar(
        [night(offset, NightState.OPEN, 300.0) for offset in range(3)],
        listing_id="b",
        name="B",
    )

    forward = board([first, second])["opportunities"]
    backward = board([second, first])["opportunities"]

    assert [entry["listing_id"] for entry in forward] == ["a", "b"]
    assert [entry["listing_id"] for entry in forward] == [
        entry["listing_id"] for entry in backward
    ]
    assert [entry["score"] for entry in forward] == [
        entry["score"] for entry in backward
    ]


def test_below_market_and_booking_window_lift_a_window_and_are_named():
    plain = calendar(
        [night(offset, NightState.OPEN, 100.0) for offset in range(3)],
        listing_id="plain",
        name="Plain",
    )

    struggling = calendar(
        [night(offset, NightState.OPEN, 100.0) for offset in range(3)],
        listing_id="struggling",
        name="Struggling",
        health=below_market_health(),
    )

    ranked = board([plain, struggling])["opportunities"]

    assert ranked[0]["listing_id"] == "struggling"
    assert "occupancy below market" in ranked[0]["reasons"]
    assert "inside normal booking window" in ranked[0]["reasons"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_ranking_returns_at_most_ten():
    calendars = [
        calendar(
            [night(0, NightState.OPEN, float(100 + index))],
            listing_id=f"listing-{index}",
            name=f"Listing {index}",
        )
        for index in range(14)
    ]

    assert len(board(calendars)["opportunities"]) == 10


# -- horizons, freshness, isolation ---------------------------------------


@pytest.mark.parametrize("horizon", ALLOWED_HORIZONS)
def test_supported_horizons_are_reported_back(horizon):
    result = board([calendar([night(0, NightState.OPEN, 100.0)])], horizon=horizon)

    assert result["horizon_days"] == horizon
    assert result["start_date"] == "2026-09-07"
    assert result["end_date"] == (START + timedelta(days=horizon - 1)).isoformat()


def test_an_unsupported_horizon_is_rejected():
    with pytest.raises(ValueError, match="Unsupported horizon"):
        board([calendar([night(0, NightState.OPEN, 100.0)])], horizon=45)


def test_horizon_filtering_drops_nights_outside_the_window():
    payload = {
        "data": [
            {
                "id": "inv-9",
                "currency": "USD",
                "last_refreshed_at": "2026-09-07T09:15:00+00:00",
                "data": [
                    {
                        "date": (START + timedelta(days=offset)).isoformat(),
                        "booking_status": "",
                        "occupancy": 0,
                        "unbookable": 0,
                        "price": 100,
                        "min_stay": 3,
                    }
                    for offset in range(30)
                ],
            }
        ]
    }

    seven = property_calendar(payload, "Invented", START, 7)

    assert len(seven.nights) == 7
    assert seven.nights[-1].stay_date == START + timedelta(days=6)


def test_a_night_the_source_never_returned_is_counted_as_missing():
    payload = {
        "data": [
            {
                "id": "inv-9",
                "data": [
                    {
                        "date": START.isoformat(),
                        "booking_status": "",
                        "occupancy": 0,
                        "unbookable": 0,
                        "price": 100,
                        "min_stay": 3,
                    }
                ],
            }
        ]
    }

    result = property_calendar(payload, "Invented", START, 7)

    assert len(result.nights) == 1
    assert result.missing_night_count == 6


def test_freshness_is_carried_per_property():
    result = board(
        [
            calendar(
                [night(0, NightState.OPEN, 100.0)],
                refreshed="2026-09-07T11:28:22+00:00",
            )
        ]
    )

    assert result["properties"][0]["last_refreshed_at"] == "2026-09-07T11:28:22+00:00"


def test_an_unreadable_refresh_stamp_becomes_unknown_not_a_guess():
    payload = {
        "data": [
            {
                "id": "inv-9",
                "last_refreshed_at": "not a timestamp",
                "data": [],
            }
        ]
    }

    assert property_calendar(payload, "Invented", START, 7).last_refreshed_at is None


def test_properties_are_analysed_in_isolation():
    busy = calendar(
        [night(offset, NightState.BOOKED, 100.0) for offset in range(4)],
        listing_id="busy",
        name="Busy",
    )

    empty = calendar(
        [night(offset, NightState.OPEN, 100.0) for offset in range(4)],
        listing_id="empty",
        name="Empty",
    )

    result = board([busy, empty])

    by_id = {entry["listing_id"]: entry for entry in result["properties"]}

    assert by_id["busy"]["occupancy_pct"] == 100.0
    assert by_id["busy"]["open_sellable_nights"] == 0
    assert by_id["empty"]["occupancy_pct"] == 0.0
    assert by_id["empty"]["open_sellable_nights"] == 4
    assert len(by_id["busy"]["calendar"]) == 4


def test_market_prose_that_does_not_parse_is_reported_as_unknown():
    payload = {
        "data": {
            "market_section": [["October", "Market data pending", "", ""]],
            "heading_section": {},
            "recommendation_section": {},
        }
    }

    assert listing_health(payload, "October") is None


# -- HTTP boundary ---------------------------------------------------------


def live_provider():
    """A provider standing in for a connected PriceLabs account.

    Fixture calendars are legitimate *here* -- a test needs deterministic
    inventory. What they may never be is the runtime default; see
    `test_the_app_package_cannot_reach_fixture_data`.
    """
    from tests.pricelabs_fixtures import FixtureVacancyProvider

    class Live(FixtureVacancyProvider):
        source_name = "PriceLabs"

        is_live = True

    return Live()


def test_vacancy_requires_authentication(api):
    assert api.anonymous().get("/vacancy").status_code == 401


def test_an_unconfigured_runtime_reports_not_connected(api, monkeypatch):
    """The default runtime state, and the point of this whole endpoint shape.

    No board, and specifically no invented properties, prices, totals,
    opportunities or occupancy. On screen a sample portfolio is
    indistinguishable from a real one, so there is no safe way to show one.
    """
    monkeypatch.delenv("PRICELABS_API_KEY", raising=False)
    monkeypatch.setattr(api.module, "vacancy_provider", None)

    response = api.client("ADMIN").get("/vacancy")

    assert response.status_code == 200

    body = response.json()

    assert body["configured"] is False
    assert body["board"] is None
    assert body["message"] == "PriceLabs is not connected to AgentGuard yet."


def test_no_provider_is_wired_without_a_credential(api, monkeypatch):
    """Absent a credential, the app wires no provider at all.

    The environment is cleared and the module reloaded inside the test rather
    than relying on the ambient one. This assertion previously passed or failed
    depending on whether the developer running it happened to have
    PRICELABS_API_KEY exported, which made it evidence of nothing.
    """
    import importlib

    monkeypatch.delenv("PRICELABS_API_KEY", raising=False)

    reloaded = importlib.reload(api.module)

    assert reloaded.vacancy_provider is None
    assert reloaded.pricelabs_recommendations is None
    assert reloaded.pricelabs_pricing_tools is None


def test_a_provider_is_wired_when_a_credential_is_present(api, monkeypatch):
    """And the converse, so the test above cannot pass for the wrong reason."""
    import importlib

    monkeypatch.setenv("PRICELABS_API_KEY", "test-only-not-a-real-key")

    reloaded = importlib.reload(api.module)

    assert reloaded.vacancy_provider is not None
    assert reloaded.pricelabs_recommendations is not None

    # Restore a keyless module so no later test inherits a configured one.
    monkeypatch.delenv("PRICELABS_API_KEY", raising=False)

    importlib.reload(api.module)


def test_a_configured_key_without_a_connector_says_so_distinctly(api, monkeypatch):
    monkeypatch.setenv("PRICELABS_API_KEY", "not-a-real-key")
    monkeypatch.setattr(api.module, "vacancy_provider", None)

    body = api.client("ADMIN").get("/vacancy").json()

    assert body["configured"] is False
    assert body["board"] is None
    assert "connector is not enabled" in body["message"]
    # The key itself never travels.
    assert "not-a-real-key" not in str(body)


@pytest.mark.parametrize("horizon", ALLOWED_HORIZONS)
def test_a_connected_provider_serves_each_supported_horizon(
    api,
    monkeypatch,
    horizon,
):
    monkeypatch.setattr(api.module, "vacancy_provider", live_provider())

    response = api.client("ADMIN").get(f"/vacancy?days={horizon}")

    assert response.status_code == 200, response.text

    body = response.json()

    assert body["configured"] is True

    board = body["board"]

    assert board["horizon_days"] == horizon
    assert board["properties"]
    assert all(len(entry["calendar"]) <= horizon for entry in board["properties"])


def test_the_route_defaults_to_sixty_days(api, monkeypatch):
    monkeypatch.setattr(api.module, "vacancy_provider", live_provider())

    body = api.client("ADMIN").get("/vacancy").json()

    assert body["board"]["horizon_days"] == 60


def test_the_route_rejects_an_unsupported_horizon(api):
    response = api.client("ADMIN").get("/vacancy?days=45")

    assert response.status_code == 400
    assert "Unsupported horizon" in response.json()["detail"]


def test_provider_failure_becomes_a_generic_gateway_error(api, monkeypatch):
    from app.connectors.pricelabs.errors import PriceLabsUnavailable

    class Failing:
        source_name = "PriceLabs"
        is_live = True

        def calendars(self, horizon_days):
            raise PriceLabsUnavailable("connection reset by 10.0.0.4:443")

    monkeypatch.setattr(api.module, "vacancy_provider", Failing())

    response = api.client("ADMIN").get("/vacancy")

    assert response.status_code == 502
    # The provider's own words never reach the client.
    assert "10.0.0.4" not in response.text
    assert response.json()["detail"] == (
        "Vacancy data could not be loaded from the provider."
    )


def test_a_provider_that_loses_its_credential_reports_not_connected(
    api,
    monkeypatch,
):
    """A credential that disappears is a configuration state, not a 5xx."""
    from app.connectors.pricelabs.errors import PriceLabsConfigurationError

    class Unconfigured:
        source_name = "PriceLabs"
        is_live = True

        def calendars(self, horizon_days):
            raise PriceLabsConfigurationError("PRICELABS_API_KEY is unset")

    monkeypatch.delenv("PRICELABS_API_KEY", raising=False)
    monkeypatch.setattr(api.module, "vacancy_provider", Unconfigured())

    response = api.client("ADMIN").get("/vacancy")

    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert "PRICELABS_API_KEY" not in response.text


# -- credentials -----------------------------------------------------------


def test_configuration_is_detected_without_revealing_the_key(monkeypatch):
    from app.connectors.pricelabs.config import is_configured, resolve_api_key

    monkeypatch.delenv("PRICELABS_API_KEY", raising=False)

    assert is_configured() is False

    monkeypatch.setenv("PRICELABS_API_KEY", "   ")

    assert is_configured() is False, "whitespace is not a credential"

    monkeypatch.setenv("PRICELABS_API_KEY", " secret-value ")

    assert is_configured() is True
    assert resolve_api_key() == "secret-value"


def test_resolving_a_missing_key_raises_a_configuration_error(monkeypatch):
    from app.connectors.pricelabs.config import resolve_api_key
    from app.connectors.pricelabs.errors import PriceLabsConfigurationError

    monkeypatch.delenv("PRICELABS_API_KEY", raising=False)

    with pytest.raises(PriceLabsConfigurationError, match="PRICELABS_API_KEY"):
        resolve_api_key()


# -- safety ----------------------------------------------------------------


def test_vacancy_is_read_only_at_the_route(api):
    """No verb other than GET reaches this path."""
    routes = [
        route
        for route in api.module.app.routes
        if getattr(route, "path", None) == "/vacancy"
    ]

    assert routes

    for route in routes:
        assert set(route.methods) <= {"GET", "HEAD"}


def test_the_app_package_cannot_reach_fixture_data():
    """Fixtures live in tests. `app/` must not be able to import them.

    This is the regression guard for the bug that made this endpoint ship a
    board of invented properties as though it were the account's own.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent

    assert not (root / "app" / "connectors" / "pricelabs" / "fixtures.py").exists()

    for path in (root / "app").rglob("*.py"):
        body = path.read_text()

        assert "FixtureVacancyProvider" not in body, path
        assert "tests.pricelabs_fixtures" not in body, path
        assert "from tests" not in body, path


def test_no_write_path_exists_in_the_vacancy_feature():
    """Every PriceLabs mutation tool is named here.

    If one is ever imported, wrapped or referenced by this feature, this fails.
    """
    import pathlib

    forbidden = (
        "update_listing_date_overrides",
        "update_group_date_overrides",
        "delete_listing_date_overrides",
        "delete_group_date_overrides",
        "update_customizations",
        "update_listing_data",
        "refresh_listing_pricing",
        "accept_nudge",
        "map_listings",
        "unmap_listings",
    )

    root = pathlib.Path(__file__).resolve().parent.parent

    sources = [
        root / "app" / "vacancy.py",
        *(root / "app" / "connectors" / "pricelabs").glob("*.py"),
    ]

    for path in sources:
        body = path.read_text()

        for name in forbidden:
            assert name not in body, f"{path.name} references {name}"


def test_the_connector_package_exposes_no_write_callable():
    from app.connectors.pricelabs import config, normalise

    verbs = ("update", "delete", "set_", "push", "sync", "write", "post")

    for module in (normalise, config):
        for name in dir(module):
            if name.startswith("_"):
                continue

            assert not name.lower().startswith(verbs), f"{module.__name__}.{name}"


# -- REST field shapes (verified live 2026-09-04) --------------------------


@pytest.mark.parametrize("occupancy", [1, 1.0])
def test_a_numeric_occupancy_is_read_whether_int_or_float(occupancy):
    """The REST API returns 1.0 where the MCP returned 1.

    An int-only check classified every REST night as UNKNOWN, which would have
    emptied the board against real data while every fixture test still passed.
    """
    assert (
        night_state(
            {"booking_status": "Booked", "occupancy": occupancy, "unbookable": 0}
        )
        is NightState.BOOKED
    )


@pytest.mark.parametrize("occupancy", [0, 0.0])
def test_a_vacant_night_is_open_whether_occupancy_is_int_or_float(occupancy):
    assert (
        night_state(
            {"booking_status": "", "occupancy": occupancy, "unbookable": 0}
        )
        is NightState.OPEN
    )


@pytest.mark.parametrize("occupancy", [True, False, "1", None])
def test_a_non_numeric_occupancy_is_unknown(occupancy):
    """A bool is an int in Python, and neither is a measurement."""
    assert (
        night_state(
            {"booking_status": "", "occupancy": occupancy, "unbookable": 0}
        )
        is NightState.UNKNOWN
    )


@pytest.mark.parametrize("unbookable", [None, "0", True])
def test_an_unreadable_unbookable_flag_is_unknown_not_open(unbookable):
    assert (
        night_state(
            {"booking_status": "", "occupancy": 0.0, "unbookable": unbookable}
        )
        is NightState.UNKNOWN
    )


def test_a_large_open_window_is_flagged_even_without_pacing_data():
    """The REST portfolio endpoint carries no booking window.

    A section that empties itself on a thinner source is worse than one that
    says less, so the window is still surfaced -- without claiming pacing.
    """
    nights = [night(offset, NightState.OPEN, 200.0) for offset in range(6)]

    entry = board([calendar(nights, health=None)])["needs_attention"][0]

    assert entry["reasons"] == ["6 consecutive open nights from 2026-09-07"]
    assert "booking window" not in entry["reasons"][0]


def test_pacing_is_named_only_when_the_source_supplied_it():
    health = ListingHealth(
        market_occupancy_pct=40.0,
        listing_occupancy_pct=45.0,
        booking_window_max_days=30,
    )

    nights = [night(offset, NightState.OPEN, 200.0) for offset in range(6)]

    entry = board([calendar(nights, health=health)])["needs_attention"][0]

    assert "inside the 30-day booking window" in entry["reasons"][0]
