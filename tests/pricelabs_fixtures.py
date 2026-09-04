"""Invented, PriceLabs-shaped calendars for development and the UI.

**This is not provider data and must never be presented as though it were.**
`FixtureVacancyProvider.is_live` is False, the route carries that flag to the
console, and the console labels the board accordingly.

The fixtures are deliberately built as raw PriceLabs-shaped payloads and pushed
through `normalise`, rather than constructed as domain objects directly. That
way the development board exercises the same translation the real transport
will, including the UNKNOWN paths.

Property names here are invented. They are not, and must not be replaced with,
any real portfolio.
"""

import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.connectors.pricelabs.models import PropertyCalendar
from app.connectors.pricelabs.normalise import listing_health, property_calendar

#: Fixed so a given day always renders the same board.
SEED = 20260904

FIXTURE_SOURCE_NAME = "PriceLabs (fixtures)"


def _health_payload(
    month: str,
    market: int,
    listing: int,
    window: tuple[int, int],
    flag: str,
    recommendations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "market_section": [
                [
                    f"{month}(High Season)",
                    f"Market Occupancy is {market}%, Your occupancy is {listing}.0%",
                    f"Market reached {market + 20}% occupancy last year",
                    (
                        "Bookings generally happen "
                        f"{window[0]}-{window[1]} days before stay"
                    ),
                ],
            ],
            "heading_section": {"flag": flag, "color": "Green", "text": ""},
            "recommendation_section": recommendations or {},
        },
    }


def _night(
    day: date,
    state: str,
    price: float | None,
    minimum_stay: int = 3,
) -> dict[str, Any]:
    """One PriceLabs-shaped row. `state` names the case being fixtured."""
    row: dict[str, Any] = {
        "date": day.isoformat(),
        "min_stay": minimum_stay,
        "price": price if price is not None else -1,
    }

    if state == "booked":
        row.update(booking_status="Booked", occupancy=1, unbookable=0)

    elif state == "checkin":
        row.update(
            booking_status="Booked (Check-In)",
            occupancy=1,
            unbookable=0,
        )

    elif state == "blocked":
        row.update(booking_status="Blocked", occupancy=0, unbookable=0)

    elif state == "orphan":
        row.update(booking_status="", occupancy=0, unbookable=1)

    elif state == "unknown":
        # The provider returned the night without a state we can read.
        row.update(booking_status="Pending sync", occupancy=0, unbookable=0)

    else:
        row.update(booking_status="", occupancy=0, unbookable=0)

    return row


def _prices_payload(
    listing_id: str,
    rows: list[dict[str, Any]],
    refreshed: str,
) -> dict[str, Any]:
    return {
        "data": [
            {
                "id": listing_id,
                "pms": "fixture",
                "currency": "USD",
                "last_refreshed_at": refreshed,
                "data": rows,
            },
        ],
    }


def _pattern(offset: int, plan: list[tuple[int, str]]) -> str:
    """Look up the state for `offset` in a run-length plan, cycling the tail."""
    cursor = 0

    for length, state in plan:
        if cursor <= offset < cursor + length:
            return state

        cursor += length

    tail = plan[-1][1]

    return tail


class FixtureVacancyProvider:
    """A `VacancyProvider` backed by invented data. Read-only, obviously."""

    source_name = FIXTURE_SOURCE_NAME

    is_live = False

    def __init__(self, today: date | None = None) -> None:
        self._today = today or datetime.now(UTC).date()

    def calendars(self, horizon_days: int) -> list[PropertyCalendar]:
        start = self._today

        refreshed = datetime.now(UTC).replace(microsecond=0).isoformat()

        stale = (
            (datetime.now(UTC) - timedelta(hours=31)).replace(microsecond=0).isoformat()
        )

        month = start.strftime("%B")

        specs = [
            # A strong performer with weekend inventory left.
            (
                "fixture-harbourview",
                "Harbourview Loft",
                [
                    (4, "booked"),
                    (3, "open"),
                    (5, "booked"),
                    (4, "open"),
                    (2, "booked"),
                    (6, "open"),
                    (5, "booked"),
                ],
                310,
                refreshed,
                _health_payload(month, 44, 61, (6, 22), "Performing well."),
                False,
            ),
            # Materially below its market: this one lands in Needs Attention.
            (
                "fixture-old-mill",
                "Old Mill Cottage",
                [
                    (3, "booked"),
                    (12, "open"),
                    (2, "booked"),
                    (9, "open"),
                    (3, "booked"),
                ],
                225,
                refreshed,
                _health_payload(
                    month,
                    38,
                    19,
                    (4, 47),
                    "Occupancy below market.",
                    {
                        "min_price": {
                            "header": "Adjust your Minimum Price",
                            "text": "",
                            "value": "135",
                        },
                    },
                ),
                False,
            ),
            # Orphan-heavy: one-night and two-night gaps behind a 3-night min.
            (
                "fixture-cedar-street",
                "Cedar Street Walk-Up",
                [
                    (3, "booked"),
                    (1, "orphan"),
                    (4, "booked"),
                    (2, "orphan"),
                    (5, "booked"),
                    (1, "orphan"),
                    (3, "open"),
                    (4, "booked"),
                ],
                395,
                refreshed,
                _health_payload(month, 41, 57, (5, 36), "Performing well."),
                False,
            ),
            # The awkward one: unknown nights, blocked nights, missing prices.
            (
                "fixture-granary",
                "Granary Studio",
                [
                    (2, "unknown"),
                    (3, "booked"),
                    (2, "blocked"),
                    (4, "open"),
                    (2, "unknown"),
                    (6, "open"),
                    (3, "booked"),
                ],
                180,
                stale,
                None,
                True,
            ),
        ]

        calendars: list[PropertyCalendar] = []

        for (
            listing_id,
            name,
            plan,
            base,
            refreshed_at,
            health_payload,
            drop_prices,
        ) in specs:
            rng = random.Random(f"{SEED}:{listing_id}")

            rows: list[dict[str, Any]] = []

            for offset in range(horizon_days):
                day = start + timedelta(days=offset)

                state = _pattern(offset, plan)

                weekend = day.weekday() in (4, 5)

                price: float | None = round(
                    base * (1.38 if weekend else 1.0) * (1 + rng.uniform(-0.12, 0.22)),
                )

                # A provider that returns a night without a usable price. The
                # board must count it and add no revenue for it.
                if drop_prices and offset % 11 == 5:
                    price = None

                rows.append(
                    _night(
                        day,
                        state,
                        price,
                        minimum_stay=3,
                    )
                )

            health = (
                listing_health(health_payload, month)
                if health_payload is not None
                else None
            )

            calendars.append(
                property_calendar(
                    _prices_payload(listing_id, rows, refreshed_at),
                    display_name=name,
                    start=start,
                    horizon_days=horizon_days,
                    health=health,
                )
            )

        return calendars
