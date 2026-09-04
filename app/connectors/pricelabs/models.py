"""Provider-neutral vacancy types.

Nothing in this module knows a PriceLabs field name. That translation lives in
`normalise.py`, so a different transport -- REST, an MCP proxy, a manual export
-- changes one module and leaves the analysis and the console untouched.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Protocol


class NightState(str, Enum):
    """What one night is, for one property.

    `UNKNOWN` is load-bearing. A night whose state could not be established is
    never folded into `OPEN`: an unknown night is not inventory we may claim is
    sellable, and reporting it as open would invent revenue.

    `BLOCKED` is deliberately distinct from `UNBOOKABLE`. Both are vacant and
    neither is sellable, but their causes differ and so does the remedy: a
    blocked night is an owner decision, while an unbookable night is a stay
    restriction. Folding blocked nights into `UNBOOKABLE` would put them in the
    "review your minimum stay" queue, which is the wrong advice for them.
    """

    BOOKED = "BOOKED"
    OPEN = "OPEN"
    UNBOOKABLE = "UNBOOKABLE"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


#: States that represent vacant inventory a guest could actually reserve.
SELLABLE_STATES: frozenset[NightState] = frozenset({NightState.OPEN})


@dataclass(frozen=True)
class Night:
    """One night of one property.

    `price` is PriceLabs' *final price* for the night -- the figure it shows on
    the calendar and recommends pushing to the PMS. It is `None` when the
    source did not carry a usable number, and a `None` price never becomes zero
    anywhere downstream: it is excluded from revenue and counted separately.
    """

    stay_date: date
    state: NightState
    price: float | None = None
    minimum_stay: int | None = None

    @property
    def is_weekend(self) -> bool:
        """Friday or Saturday night. Documented convention, used everywhere."""
        return self.stay_date.weekday() in (4, 5)


@dataclass(frozen=True)
class ListingHealth:
    """Pacing and market context for one property, as the provider reports it.

    Every field is optional: a provider that cannot answer leaves it `None`
    rather than supplying a default that would read as a measurement.
    """

    month_label: str | None = None
    #: True when the occupancy figures describe one calendar month. The REST
    #: portfolio endpoint reports rolling 7/30/60-day windows, which are not
    #: months; a rolling figure must never be shown as a month's benchmark.
    is_month_scoped: bool = False
    market_occupancy_pct: float | None = None
    listing_occupancy_pct: float | None = None
    booking_window_min_days: int | None = None
    booking_window_max_days: int | None = None
    provider_flag: str | None = None
    provider_recommendations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PropertyCalendar:
    """One property's nights over a horizon, plus its freshness and health."""

    listing_id: str
    display_name: str
    nights: tuple[Night, ...]
    currency: str = "USD"
    last_refreshed_at: str | None = None
    health: ListingHealth | None = None
    #: Nights the source did not return at all, distinct from nights it
    #: returned without a price. Both are unknowns; they are not the same one.
    missing_night_count: int = 0
    notes: tuple[str, ...] = field(default=())


class VacancyProvider(Protocol):
    """Where a vacancy board's raw calendars come from.

    Read-only by construction: there is no write method to call, and absence is
    the safety property -- see the connector invariants in AGENTS.md.
    """

    @property
    def source_name(self) -> str:
        """Short label for the console, e.g. "PriceLabs"."""

    @property
    def is_live(self) -> bool:
        """False when the calendars are fixtures rather than provider data.

        The console renders this: a board built from fixtures must never be
        presented as though it came from the account.
        """

    def calendars(self, horizon_days: int) -> list[PropertyCalendar]:
        """Calendars for every property, covering `horizon_days` from today."""
