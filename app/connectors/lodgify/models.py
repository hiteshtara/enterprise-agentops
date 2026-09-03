"""The only shapes this connector will emit.

Every field here is constructed explicitly from a named upstream field. There
is no passthrough, no `**rest`, and no `dict(response)` -- so an upstream
payload that grows a booking id, a guest name or a channel/source field cannot
reach the agent, the audit log, the run trace or the browser.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AvailabilityPeriod:
    """One contiguous span of a single availability state.

    `end` is inclusive, matching the provider's own calendar convention.
    """

    start: str
    end: str
    available: bool

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "available": self.available}


@dataclass(frozen=True)
class AvailabilityResult:
    property_slug: str
    start: str
    end: str
    periods: tuple[AvailabilityPeriod, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "property_slug": self.property_slug,
            "start": self.start,
            "end": self.end,
            "periods": [period.to_dict() for period in self.periods],
        }


@dataclass(frozen=True)
class QuoteResult:
    property_slug: str
    arrival: str
    departure: str
    guest_count: int
    currency: str
    accommodation_amount: float
    cleaning_fee: float
    taxes: float
    total: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "property_slug": self.property_slug,
            "arrival": self.arrival,
            "departure": self.departure,
            "guest_count": self.guest_count,
            "currency": self.currency,
            "accommodation_amount": self.accommodation_amount,
            "cleaning_fee": self.cleaning_fee,
            "taxes": self.taxes,
            "total": self.total,
        }


def unknown(reason: str, message: str, **context: Any) -> dict[str, Any]:
    """The fail-closed answer.

    Returned whenever the provider could not give a usable answer. It carries
    no `available` key at all, so no caller -- model or human -- can read a
    provider failure as an open calendar.
    """
    return {
        "ok": False,
        "status": "unknown",
        "reason": reason,
        "message": message,
        **context,
    }


def declined(reason: str, message: str, **context: Any) -> dict[str, Any]:
    """A known "no" from a provider business rule, distinct from unknown."""
    return {
        "ok": False,
        "status": "declined",
        "reason": reason,
        "message": message,
        **context,
    }
