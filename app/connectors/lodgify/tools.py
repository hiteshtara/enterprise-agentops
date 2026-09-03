"""The Lodgify capabilities exposed to the agent.

Three read-only tools. The model chooses a property by slug from a closed enum
and never supplies a provider identifier; this layer resolves the numeric ids.

Validation vs. provider failure are deliberately different outcomes:

  * A bad argument (unknown slug, malformed date, window too wide, guest count
    out of range) raises ValueError. The runtime treats that as recoverable,
    tells the model what was wrong, and lets it correct itself.
  * A provider that times out, errors or answers unusably returns a structured
    "unknown" result. It is never raised as a bad argument, and it never
    contains `available`, so a failure cannot be read as an open calendar.
"""

from datetime import date
from typing import Any

from app.connectors.lodgify.client import LodgifyClient
from app.connectors.lodgify.config import (
    LODGIFY_PROPERTIES,
    LODGIFY_SLUGS,
    MAX_AVAILABILITY_DAYS,
    MAX_GUESTS,
    MIN_GUESTS,
    STANDALONE_PROPERTIES,
    find_lodgify_property,
)
from app.connectors.lodgify.errors import (
    LodgifyConfigurationError,
    LodgifyRejected,
    LodgifyUnavailable,
)
from app.connectors.lodgify.models import (
    AvailabilityResult,
    QuoteResult,
    declined,
    unknown,
)


def parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string in YYYY-MM-DD form.")

    try:
        return date.fromisoformat(value)

    except ValueError as exc:
        raise ValueError(
            f"{field} must be a date in YYYY-MM-DD form, got {value!r}."
        ) from exc


def validate_guest_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"guest_count must be an integer, got {type(value).__name__}.")

    if value < MIN_GUESTS or value > MAX_GUESTS:
        raise ValueError(
            f"guest_count must be between {MIN_GUESTS} and {MAX_GUESTS}, got {value}."
        )

    return value


class LodgifyTools:
    """Adapter between the tool registry and the Lodgify client."""

    def __init__(self, client: LodgifyClient) -> None:
        self._client = client

    # -- list_properties ---------------------------------------------------

    def list_properties(self) -> list[dict[str, Any]]:
        """Every configured property and whether it is bookable via Lodgify.

        No provider call and no provider identifiers: the model gets the slugs
        it is allowed to use and nothing it could use to address the API
        directly.
        """
        properties = [
            {
                "slug": prop.slug,
                "name": prop.display_name,
                "lodgify_connected": True,
            }
            for prop in LODGIFY_PROPERTIES
        ]

        properties.extend(
            {
                "slug": standalone.slug,
                "name": standalone.display_name,
                "lodgify_connected": False,
            }
            for standalone in STANDALONE_PROPERTIES
        )

        return properties

    # -- get_property_availability ----------------------------------------

    def get_property_availability(
        self,
        property_slug: str,
        start: str,
        end: str,
    ) -> dict[str, Any]:
        prop = find_lodgify_property(property_slug)

        start_date = parse_date(start, "start")
        end_date = parse_date(end, "end")

        if end_date <= start_date:
            raise ValueError("end must be after start.")

        span = (end_date - start_date).days

        if span > MAX_AVAILABILITY_DAYS:
            raise ValueError(
                f"The availability window must be {MAX_AVAILABILITY_DAYS} days or "
                f"fewer, got {span}. Ask about a shorter range."
            )

        try:
            periods = self._client.get_availability(
                prop.lodgify_property_id, start, end
            )

        except LodgifyConfigurationError:
            return unknown(
                "not_configured",
                "The Lodgify connector is not configured, so availability cannot "
                "be checked.",
                property_slug=property_slug,
            )

        except LodgifyUnavailable as exc:
            # Fail closed. No `available` key appears anywhere in this result.
            return unknown(
                "provider_unavailable",
                f"Availability could not be confirmed: {exc}",
                property_slug=property_slug,
                start=start,
                end=end,
            )

        return AvailabilityResult(
            property_slug=property_slug,
            start=start,
            end=end,
            periods=periods,
        ).to_dict()

    # -- get_property_quote ------------------------------------------------

    def get_property_quote(
        self,
        property_slug: str,
        arrival: str,
        departure: str,
        guest_count: int,
    ) -> dict[str, Any]:
        prop = find_lodgify_property(property_slug)

        arrival_date = parse_date(arrival, "arrival")
        departure_date = parse_date(departure, "departure")

        if departure_date <= arrival_date:
            raise ValueError("departure must be after arrival.")

        guests = validate_guest_count(guest_count)

        try:
            priced = self._client.get_quote(
                property_id=prop.lodgify_property_id,
                room_type_id=prop.room_type_id,
                arrival=arrival,
                departure=departure,
                guest_count=guests,
            )

        except LodgifyConfigurationError:
            return unknown(
                "not_configured",
                "The Lodgify connector is not configured, so pricing cannot be "
                "retrieved.",
                property_slug=property_slug,
            )

        except LodgifyRejected as exc:
            # A known "no" from a booking rule -- not a failure to answer.
            return declined(
                exc.reason,
                str(exc),
                property_slug=property_slug,
                arrival=arrival,
                departure=departure,
            )

        except LodgifyUnavailable as exc:
            return unknown(
                "provider_unavailable",
                f"Pricing could not be retrieved: {exc}",
                property_slug=property_slug,
            )

        return QuoteResult(
            property_slug=property_slug,
            arrival=arrival,
            departure=departure,
            guest_count=guests,
            currency=str(priced["currency"]),
            accommodation_amount=float(priced["accommodation_amount"]),
            cleaning_fee=float(priced["cleaning_fee"]),
            taxes=float(priced["taxes"]),
            total=float(priced["total"]),
        ).to_dict()


# -- tool schemas ---------------------------------------------------------

DATE_DESCRIPTION = "A calendar date in YYYY-MM-DD form."

PROPERTY_SLUG_SCHEMA = {
    "type": "string",
    "enum": list(LODGIFY_SLUGS),
    "description": (
        "Which property to ask about. Only these Lodgify-connected properties "
        "can be queried."
    ),
}


LIST_PROPERTIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

AVAILABILITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "property_slug": PROPERTY_SLUG_SCHEMA,
        "start": {"type": "string", "description": DATE_DESCRIPTION},
        "end": {
            "type": "string",
            "description": (
                f"{DATE_DESCRIPTION} Must be after start, and no more than "
                f"{MAX_AVAILABILITY_DAYS} days later."
            ),
        },
    },
    "required": ["property_slug", "start", "end"],
    "additionalProperties": False,
}

QUOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "property_slug": PROPERTY_SLUG_SCHEMA,
        "arrival": {"type": "string", "description": DATE_DESCRIPTION},
        "departure": {
            "type": "string",
            "description": f"{DATE_DESCRIPTION} Must be after arrival.",
        },
        "guest_count": {
            "type": "integer",
            "minimum": MIN_GUESTS,
            "maximum": MAX_GUESTS,
            "description": "Total number of guests.",
        },
    },
    "required": ["property_slug", "arrival", "departure", "guest_count"],
    "additionalProperties": False,
}
