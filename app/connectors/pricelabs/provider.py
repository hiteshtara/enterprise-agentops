"""The live PriceLabs vacancy provider.

Combines the two verified read endpoints into `PropertyCalendar` objects:
`GET /v1/listings` supplies the portfolio, its display names and its occupancy
against market; `POST /v1/listing_prices` supplies the nightly rows.

Every field it reads was observed on a live response on 2026-09-04. Nothing is
inferred from documentation alone, and no field is forwarded that was not named
here -- an upstream payload that grows a guest name cannot reach the board.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.connectors.pricelabs.client import PriceLabsClient
from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.connectors.pricelabs.models import PropertyCalendar
from app.connectors.pricelabs.normalise import (
    listing_health_from_listing,
    property_calendar,
)

SOURCE_NAME = "PriceLabs"


class PriceLabsVacancyProvider:
    """Live inventory for the Vacancy board. Read-only."""

    source_name = SOURCE_NAME

    is_live = True

    def __init__(
        self,
        client: PriceLabsClient,
        today: date | None = None,
    ) -> None:
        self._client = client
        self._today = today

    def _start(self) -> date:
        return self._today or datetime.now(UTC).date()

    def calendars(self, horizon_days: int) -> list[PropertyCalendar]:
        start = self._start()

        end = start + timedelta(days=horizon_days - 1)

        listings = self._client.listings()

        wanted: list[tuple[str, str]] = []

        meta: dict[str, dict[str, Any]] = {}

        for row in listings:
            listing_id = row.get("id")
            pms = row.get("pms")

            if not isinstance(listing_id, str) or not isinstance(pms, str):
                continue

            # A hidden listing is not part of the operator's live portfolio.
            if row.get("isHidden") is True:
                continue

            wanted.append((listing_id, pms))

            meta[listing_id] = row

        if not wanted:
            return []

        priced = self._client.listing_prices(
            wanted,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
        )

        calendars: list[PropertyCalendar] = []

        for entry in priced:
            listing_id = entry.get("id")

            if not isinstance(listing_id, str):
                continue

            row = meta.get(listing_id, {})

            name = row.get("name")

            try:
                calendars.append(
                    property_calendar(
                        {"data": [entry]},
                        display_name=(
                            name if isinstance(name, str) and name else listing_id
                        ),
                        start=start,
                        horizon_days=horizon_days,
                        health=listing_health_from_listing(row, horizon_days),
                    )
                )

            except ValueError as exc:
                raise PriceLabsUnavailable(
                    "PriceLabs prices had an unexpected shape"
                ) from exc

        return calendars
