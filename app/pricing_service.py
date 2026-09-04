"""Assembles live pricing recommendations. Reads only.

One place where the reads are gathered and handed to `pricing_recommendations`,
so the decision rules stay free of I/O and remain testable against invented
state.
"""

import collections
import datetime
import statistics
from typing import Any

from app.connectors.pricelabs.client import PriceLabsClient
from app.connectors.pricelabs.normalise import parse_market_series
from app.pricing_config import bands_for
from app.pricing_policy import MarketState, Recommendation
from app.pricing_recommendations import recommend_night

HORIZON_DAYS = 60

LEAD_BANDS = ((0, 3), (4, 7), (8, 14), (15, 30), (31, 60), (61, 10**6))


def lead_band(days: int) -> tuple[int, int]:
    for low, high in LEAD_BANDS:
        if low <= days <= high:
            return low, high

    return LEAD_BANDS[-1]


def band_label(days: int) -> str:
    low, high = lead_band(days)

    return f"{low}-{high}d" if high < 10**6 else f"{low}d+"


def _percent(raw: Any) -> float | None:
    try:
        return float(str(raw).replace("%", "").strip())

    except (TypeError, ValueError):
        return None


class PricingRecommendationService:
    """Builds today's recommendations from live PriceLabs reads."""

    def __init__(
        self,
        client: PriceLabsClient,
        pms: str = "lodgify",
        today: datetime.date | None = None,
    ) -> None:
        self._client = client
        self._pms = pms
        self._today = today

    def _start(self) -> datetime.date:
        return self._today or datetime.datetime.now(datetime.UTC).date()

    def build(self, history: dict[str, list[tuple[int, float]]] | None = None):
        """Every night's recommendation across the horizon."""
        start = self._start()
        end = start + datetime.timedelta(days=HORIZON_DAYS - 1)

        listings = {
            row["id"]: row
            for row in self._client.listings()
            if isinstance(row.get("id"), str) and row.get("isHidden") is not True
        }

        priced = self._client.listing_prices(
            [(lid, row.get("pms") or self._pms) for lid, row in listings.items()],
            start.isoformat(),
            end.isoformat(),
        )

        history = history or {}

        out: list[Recommendation] = []

        for entry in priced:
            lid = entry.get("id")

            listing = listings.get(lid)

            if listing is None:
                continue

            bedrooms = listing.get("no_of_bedrooms")

            try:
                market = parse_market_series(
                    self._client.neighborhood_data(lid, self._pms),
                    bedrooms if isinstance(bedrooms, int) else None,
                )

            except Exception:  # noqa: BLE001 - market is optional evidence
                # No market reference means LOW confidence, which means HOLD.
                # Losing it must never take the whole board down.
                market = {}

            # Deliberately not guarded. An unknown pin state must never be
            # reported as "not pinned": every pinned night would then look like
            # ordinary open inventory and could be repriced on that basis.
            overrides = {
                row.get("date")
                for row in self._client.overrides(lid, self._pms)
                if isinstance(row.get("date"), str)
            }

            gap = None

            listing_occ = _percent(listing.get("occupancy_next_60"))
            market_occ = _percent(listing.get("market_occupancy_next_60"))

            if listing_occ is not None and market_occ is not None:
                gap = listing_occ - market_occ

            by_band = collections.defaultdict(list)

            for days, adr in history.get(lid, []):
                by_band[lead_band(days)].append(adr)

            for row in entry.get("data") or []:
                day = row.get("date")

                if not isinstance(day, str):
                    continue

                try:
                    stay = datetime.date.fromisoformat(day)

                except ValueError:
                    continue

                days_out = (stay - start).days

                reference = market.get(day, {})

                samples = by_band.get(lead_band(days_out), [])

                state = MarketState(
                    current_price=_number(row.get("price")),
                    market_p25=reference.get("p25"),
                    market_booked_median=reference.get("booked_median"),
                    market_occupancy=reference.get("market_occupancy"),
                    listing_occupancy=listing_occ,
                    demand=row.get("demand_desc"),
                    pickup_7_days=None,
                    pinned_price=(
                        _number(row.get("price")) if day in overrides else None
                    ),
                    last_refreshed_at=entry.get("last_refreshed_at"),
                )

                out.append(
                    recommend_night(
                        listing_id=lid,
                        display_name=str(listing.get("name") or lid),
                        stay_date=stay,
                        days_out=days_out,
                        state=state,
                        occupancy_gap=gap,
                        history_adr=(
                            statistics.median(samples) if len(samples) >= 3 else None
                        ),
                        history_count=len(samples),
                        lead_band=band_label(days_out),
                        is_pinned=day in overrides,
                        is_open=(
                            row.get("booking_status") == ""
                            and row.get("unbookable") == 0
                        ),
                    )
                )

        return out


def _number(raw: Any) -> float | None:
    try:
        value = float(raw)

    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def bands_payload() -> list[dict[str, Any]]:
    """Owner bands for the console, so limits are visible before approving.

    Resolved through `bands_for` rather than read off the static table: the
    per-listing switch lives in the environment, and a console that reported a
    safety control as off while it was on would misinform the one person the
    control exists to protect.
    """
    from app.pricing_config import BANDS

    resolved = [bands_for(entry.listing_id) or entry for entry in BANDS]

    return [
        {
            "listing_id": band.listing_id,
            "slug": band.slug,
            "display_name": band.display_name,
            "hard_floor": band.hard_floor,
            "normal_floor": band.normal_floor,
            "auto_raise_ceiling": band.auto_raise_ceiling,
            "absolute_ceiling": band.absolute_ceiling,
            "automation_enabled": band.automation_enabled,
            "raise_requires_human": band.raise_requires_human,
        }
        for band in resolved
    ]


def bands_for_listing(listing_id: str):
    return bands_for(listing_id)
