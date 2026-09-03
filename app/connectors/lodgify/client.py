"""HTTP client for the Lodgify Public API. Read-only.

Only two endpoints are implemented, both GET. The connector has no method that
creates, changes or cancels anything -- the absence is the safety property, so
do not add a write here without a milestone that says so.

Parameter spellings below were verified against the live account by the
Priyanka Homes project and are load-bearing:

  * availability uses `start`/`end`. `from`/`to` are silently accepted and
    ignored, returning parameter-invariant placeholder data -- a wrong answer
    that looks like a right one.
  * quote uses `arrival`/`departure` and `roomTypes[0].guestbreakdown.adults`.
    The underscored `guest_breakdown` returns a 500, not a clean 400.
"""

from typing import Any

import httpx

from app.connectors.lodgify.config import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from app.connectors.lodgify.errors import LodgifyRejected, LodgifyUnavailable
from app.connectors.lodgify.models import AvailabilityPeriod

# Provider business-rule messages, mapped to stable reasons. Matched on the
# provider's text because the API does not expose a machine-readable code for
# every case; anything unmatched becomes "other" rather than being guessed at.
REJECTION_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "minimum stay",
        "min_stay",
        "This home requires a longer minimum stay for those dates.",
    ),
    (
        "already booked",
        "unavailable",
        "Those dates are not available.",
    ),
    (
        "unavailable",
        "unavailable",
        "Those dates are not available.",
    ),
    (
        "number of people",
        "guest_limit",
        "This home cannot accommodate that many guests.",
    ),
    (
        "too high",
        "guest_limit",
        "This home cannot accommodate that many guests.",
    ),
    (
        "invalid dates",
        "invalid_dates",
        "Those dates were not accepted by the provider.",
    ),
)

ROOM_RATE_TYPE = 0

FEES_TYPE = 2

TAXES_TYPE = 4


def classify_rejection(message: str) -> LodgifyRejected:
    """Translate a provider 400 into a safe, stable reason.

    The provider's own text is never surfaced -- only our translation of it.
    """
    lowered = (message or "").lower()

    for needle, reason, safe_message in REJECTION_RULES:
        if needle in lowered:
            return LodgifyRejected(reason, safe_message)

    return LodgifyRejected(
        "other",
        "The provider could not price those dates.",
    )


class LodgifyClient:
    """Talks to Lodgify and returns only sanitized values.

    The credential is supplied by a callable so it is resolved per call and
    never stored on the instance, logged, or included in any return value.
    """

    def __init__(
        self,
        api_key_provider,
        transport: httpx.BaseTransport | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        base_url: str = BASE_URL,
    ) -> None:
        self._api_key_provider = api_key_provider
        self._transport = transport
        self._timeout = timeout
        self._base_url = base_url

    def get(self, path: str, params: dict[str, str]) -> Any:
        """One bounded GET. No retries: a slow provider must not stack calls."""
        headers = {
            "X-ApiKey": self._api_key_provider(),
            "accept": "application/json",
            "User-Agent": USER_AGENT,
        }

        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.get(path, params=params, headers=headers)

        except httpx.TimeoutException as exc:
            raise LodgifyUnavailable("The provider did not respond in time.") from exc

        except httpx.HTTPError as exc:
            # Deliberately does not include str(exc): a transport error can
            # contain the full request URL, and the URL is not secret but the
            # habit of echoing provider internals is what leaks one later.
            raise LodgifyUnavailable("The provider could not be reached.") from exc

        if response.status_code == 400:
            raise classify_rejection(self.message_of(response))

        if response.status_code >= 400:
            raise LodgifyUnavailable(
                f"The provider returned an error status ({response.status_code})."
            )

        try:
            return response.json()

        except ValueError as exc:
            raise LodgifyUnavailable(
                "The provider returned a response that could not be read."
            ) from exc

    def message_of(self, response: httpx.Response) -> str:
        try:
            body = response.json()

        except ValueError:
            return ""

        return body.get("message", "") if isinstance(body, dict) else ""

    # -- availability ------------------------------------------------------

    def get_availability(
        self,
        property_id: int,
        start: str,
        end: str,
    ) -> tuple[AvailabilityPeriod, ...]:
        """Sanitized availability periods for one property.

        Raises LodgifyUnavailable on any failure. It never returns an empty
        tuple to mean "failed" -- an empty tuple is a real answer (the provider
        reported no periods), and callers must be able to tell them apart.
        """
        payload = self.get(
            f"/v2/availability/{property_id}",
            {"start": start, "end": end, "includeDetails": "true"},
        )

        if not isinstance(payload, list):
            raise LodgifyUnavailable(
                "The provider returned availability in an unexpected shape."
            )

        periods: list[AvailabilityPeriod] = []

        for row in payload:
            if not isinstance(row, dict):
                continue

            for raw in row.get("periods") or []:
                if not isinstance(raw, dict):
                    continue

                start_value = raw.get("start")
                end_value = raw.get("end")
                available = raw.get("available")

                if not isinstance(start_value, str) or not isinstance(end_value, str):
                    raise LodgifyUnavailable(
                        "The provider returned availability in an unexpected shape."
                    )

                if not isinstance(available, (int, float)) or isinstance(
                    available, bool
                ):
                    raise LodgifyUnavailable(
                        "The provider returned availability in an unexpected shape."
                    )

                # Only these three fields are read. Booking rows, channel
                # calendars and guest data present on the real response are
                # dropped here, not filtered downstream.
                periods.append(
                    AvailabilityPeriod(
                        start=start_value,
                        end=end_value,
                        available=available > 0,
                    )
                )

        return tuple(periods)

    # -- quote -------------------------------------------------------------

    def get_quote(
        self,
        property_id: int,
        room_type_id: int,
        arrival: str,
        departure: str,
        guest_count: int,
    ) -> dict[str, float | str]:
        """Sanitized pricing for one stay.

        Raises LodgifyRejected for a business-rule "no", LodgifyUnavailable for
        anything the provider could not answer usably.
        """
        payload = self.get(
            f"/v2/quote/{property_id}",
            {
                "arrival": arrival,
                "departure": departure,
                "roomTypes[0].Id": str(room_type_id),
                "roomTypes[0].guestbreakdown.adults": str(guest_count),
            },
        )

        quote = payload[0] if isinstance(payload, list) and payload else payload

        if not isinstance(quote, dict):
            raise LodgifyUnavailable(
                "The provider returned a quote in an unexpected shape."
            )

        room_types = quote.get("room_types")
        first_room = (
            room_types[0] if isinstance(room_types, list) and room_types else {}
        )
        price_types = (
            first_room.get("price_types") if isinstance(first_room, dict) else []
        )

        def subtotal(type_code: int) -> float | None:
            for entry in price_types or []:
                if isinstance(entry, dict) and entry.get("type") == type_code:
                    value = entry.get("subtotal")

                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        return float(value)

            return None

        accommodation = subtotal(ROOM_RATE_TYPE)
        total = quote.get("total_excluding_vat")
        currency = quote.get("currency_code")

        if (
            accommodation is None
            or not isinstance(total, (int, float))
            or isinstance(total, bool)
            or not isinstance(currency, str)
            or not currency
        ):
            raise LodgifyUnavailable(
                "The provider returned a quote in an unexpected shape."
            )

        # Cancellation policy text, scheduled payments, rental agreements, and
        # every identifier in the real response are deliberately not read.
        return {
            "currency": currency,
            "accommodation_amount": accommodation,
            "cleaning_fee": subtotal(FEES_TYPE) or 0.0,
            "taxes": subtotal(TAXES_TYPE) or 0.0,
            "total": float(total),
        }
