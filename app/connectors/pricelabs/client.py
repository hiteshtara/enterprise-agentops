"""HTTP client for the PriceLabs REST API. Read-only.

Two endpoints, both reads. There is no method here that changes a price, an
override, a minimum stay, availability or a reservation -- the absence is the
safety property, so do not add one without a milestone that says so.

Verified live against the account on 2026-09-04:

  * ``GET  /v1/listings`` -- the portfolio, with ``occupancy_next_{7,30,60}``
    and ``market_occupancy_next_{7,30,60}`` as native fields, plus
    ``last_refreshed_at`` per listing.
  * ``POST /v1/listing_prices`` -- nightly rows. **POST is how this read is
    spelled**; it carries the listing selection in a body and returns prices.
    It writes nothing. All seven listings can be requested in one call.

Two spellings are load-bearing and were confirmed by observation:

  * A request without a ``User-Agent`` is rejected by the edge with 403 before
    it reaches the API, even with a valid key.
  * The price body uses ``dateFrom``/``dateTo`` inside each ``listings`` entry.
"""

from typing import Any

import httpx

from app.connectors.pricelabs.config import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from app.connectors.pricelabs.errors import PriceLabsUnavailable

LISTINGS_PATH = "/v1/listings"

LISTING_PRICES_PATH = "/v1/listing_prices"

#: Date-specific overrides. `pms` is a required query param on the read: without
#: it the API answers 400, not an empty list. Verified live 2026-09-04.
OVERRIDES_PATH = "/v1/listings/{listing_id}/overrides"

#: Neighbourhood comp-set data. Note the American spelling: `/v1/neighbourhood_data`
#: is a 404. Both `listing_id` and `pms` are required query params; without them
#: the API answers 400. Verified live 2026-09-04.
NEIGHBORHOOD_PATH = "/v1/neighborhood_data"


class PriceLabsClient:
    """Read-only access to the PriceLabs REST API.

    Takes a resolver rather than a key so the credential is fetched at call
    time and never stored on the instance.
    """

    def __init__(self, api_key_provider) -> None:
        self._api_key_provider = api_key_provider

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._api_key_provider(),
            "Accept": "application/json",
            # Required: the edge rejects an absent User-Agent with 403 even
            # when the key is valid.
            "User-Agent": USER_AGENT,
        }

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = httpx.request(
                method,
                f"{BASE_URL}{path}",
                headers=self._headers(),
                json=json_body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

        except httpx.HTTPError as exc:
            raise PriceLabsUnavailable(
                f"PriceLabs could not be reached: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            # The provider's body is never forwarded; only the status class.
            raise PriceLabsUnavailable(
                f"PriceLabs answered {response.status_code}"
            )

        try:
            return response.json()

        except ValueError as exc:
            raise PriceLabsUnavailable("PriceLabs answered unparsable JSON") from exc

    def listings(self) -> list[dict[str, Any]]:
        """Every listing on the account."""
        payload = self._request("GET", LISTINGS_PATH)

        if not isinstance(payload, dict):
            raise PriceLabsUnavailable("PriceLabs listings had an unexpected shape")

        rows = payload.get("listings")

        if not isinstance(rows, list):
            raise PriceLabsUnavailable("PriceLabs listings had an unexpected shape")

        return [row for row in rows if isinstance(row, dict)]

    def overrides(
        self,
        listing_id: str,
        pms: str,
    ) -> list[dict[str, Any]]:
        """Every date-specific override on a listing. Read-only.

        Returns all of them, not just the near ones, so callers filter by date
        themselves rather than assuming a horizon.
        """
        payload = self._request(
            "GET",
            OVERRIDES_PATH.format(listing_id=listing_id) + f"?pms={pms}",
        )

        if not isinstance(payload, dict):
            raise PriceLabsUnavailable("PriceLabs overrides had an unexpected shape")

        rows = payload.get("overrides")

        if not isinstance(rows, list):
            raise PriceLabsUnavailable("PriceLabs overrides had an unexpected shape")

        return [row for row in rows if isinstance(row, dict)]

    def neighborhood_data(
        self,
        listing_id: str,
        pms: str,
    ) -> dict[str, Any]:
        """Comp-set percentile prices and occupancy, per stay date.

        The payload is column-oriented: `X_values` is the date axis and
        `Y_values` is one series per label in `Labels`, keyed by bedroom
        category. `parse_market_series` turns that into per-date readings.
        """
        payload = self._request(
            "GET",
            f"{NEIGHBORHOOD_PATH}?listing_id={listing_id}&pms={pms}",
        )

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise PriceLabsUnavailable(
                "PriceLabs neighbourhood data had an unexpected shape"
            )

        return payload["data"]

    def listing_prices(
        self,
        listings: list[tuple[str, str]],
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        """Nightly rows for each `(listing_id, pms)` over an inclusive range.

        A read, despite the verb: the body selects listings and the response
        carries prices. Nothing is created or changed.
        """
        if not listings:
            return []

        payload = self._request(
            "POST",
            LISTING_PRICES_PATH,
            json_body={
                "listings": [
                    {
                        "id": listing_id,
                        "pms": pms,
                        "dateFrom": date_from,
                        "dateTo": date_to,
                    }
                    for listing_id, pms in listings
                ],
            },
        )

        if not isinstance(payload, list):
            raise PriceLabsUnavailable("PriceLabs prices had an unexpected shape")

        return [entry for entry in payload if isinstance(entry, dict)]
