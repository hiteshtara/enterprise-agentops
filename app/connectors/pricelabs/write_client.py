"""The only module in AgentGuard that can change a price at PriceLabs.

Everything here is deliberately narrow. There is one class, it exposes two
operations, and both are gated by two independent switches before a request is
built. The model cannot reach it: the tool that calls it is registered
`model_callable=False`, so `ToolRegistry.definitions()` never advertises it.

Verified against the PriceLabs Customer API on 2026-09-04:

  POST   /v1/listings/{listing_id}/overrides   create or update a date override
  DELETE /v1/listings/{listing_id}/overrides   remove date overrides
  GET    /v1/listings/{listing_id}/overrides?pms=…   read them back

Three outcomes, never two
-------------------------
A price change is visible to guests and channels, and PriceLabs offers no
idempotency key. A timeout is therefore ambiguous: the change may already have
landed. So this module snapshots the overrides before acting, acts exactly
once, re-reads afterwards and diffs. When the re-read cannot settle the
question the outcome is `UNKNOWN_WRITE_STATE`, which is **not a failure and is
never retried** -- it asks for a person.

Why a LOWER or RAISE carries an expiry
--------------------------------------
Setting a price through this API means writing a date-specific override, and a
fixed override is exactly what stops PriceLabs pricing a date dynamically --
the failure this whole feature was built to surface. Every price-setting write
therefore carries `lead_time_expiry`, so the override hands the date back to
dynamic pricing on its own. An override with no expiry is a pin, and pins are
created by people, not by this code.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from app.connectors.pricelabs.client import OVERRIDES_PATH, PriceLabsClient
from app.connectors.pricelabs.config import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.pricing_config import writes_enabled

#: Days before arrival at which a price-setting override expires by itself.
#: Chosen so a nudge cannot silently become a permanent pin.
DEFAULT_LEAD_TIME_EXPIRY_DAYS = 3


class WriteOutcome(str, Enum):
    """The three outcomes of an externally visible price change.

    `CONFIRMED_APPLIED` means exactly one thing: **PriceLabs accepted and
    persisted the requested override**, confirmed by reading it back. It does
    *not* mean the channel price changed, and it must never be reported that
    way. PriceLabs recomputes its price series on its own refresh cycle, so a
    stored override and a live guest-facing rate are two separate facts
    established by two separate observations. The first live write on
    2026-09-04 demonstrated the gap: the override was stored at $246 while the
    price series still read $239.

    `UNKNOWN_WRITE_STATE` is not a failure. The change may already be live on
    every channel, so retrying it is not a retry -- it is a second change to a
    real listing. It requires a person to look.
    """

    CONFIRMED_APPLIED = "CONFIRMED_APPLIED"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"
    UNKNOWN_WRITE_STATE = "UNKNOWN_WRITE_STATE"


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    message: str
    stay_date: str
    old_price: float | None = None
    new_price: float | None = None

    @property
    def needs_human(self) -> bool:
        return self.outcome is WriteOutcome.UNKNOWN_WRITE_STATE


class PricingWritesDisabled(Exception):
    """A switch is off. Nothing was sent, and nothing was attempted."""


class PriceLabsWriteClient:
    """One override write, gated twice and verified by re-reading."""

    def __init__(
        self,
        reader: PriceLabsClient,
        api_key_provider,
    ) -> None:
        self._reader = reader
        self._api_key_provider = api_key_provider

    def _guard(self, automation_enabled: bool) -> None:
        """Both switches, checked before a request object is even built."""
        if not writes_enabled():
            raise PricingWritesDisabled(
                "ENABLE_PRICING_WRITES is not enabled; no price was changed."
            )

        if not automation_enabled:
            raise PricingWritesDisabled(
                "Pricing automation is not enabled for this listing; "
                "no price was changed."
            )

    def _send(self, method: str, listing_id: str, body: dict[str, Any]) -> bool:
        """One request. True if the provider confirmed it, False if it refused.

        Raises `PriceLabsUnavailable` only when the outcome is genuinely
        unknown -- a timeout or transport failure, where the change may have
        landed anyway.
        """
        try:
            response = httpx.request(
                method,
                f"{BASE_URL}{OVERRIDES_PATH.format(listing_id=listing_id)}",
                headers={
                    "X-API-Key": self._api_key_provider(),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

        except httpx.HTTPError as exc:
            # The request may already have been applied. Never a clean failure.
            raise PriceLabsUnavailable(
                f"PriceLabs did not answer: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 500:
            raise PriceLabsUnavailable(
                f"PriceLabs answered {response.status_code}"
            )

        # A 4xx is the provider understanding and refusing: nothing changed.
        return response.status_code < 400

    def _override_for(
        self,
        listing_id: str,
        pms: str,
        stay_date: str,
    ) -> dict[str, Any] | None:
        for row in self._reader.overrides(listing_id, pms):
            if row.get("date") == stay_date:
                return row

        return None

    def _verify(
        self,
        listing_id: str,
        pms: str,
        stay_date: str,
        expect_present: bool,
        expected_price: float | None,
        old_price: float | None,
        acknowledged: bool,
    ) -> WriteResult:
        """Re-read and diff. The provider's own answer is never trusted alone."""
        try:
            after = self._override_for(listing_id, pms, stay_date)

        except PriceLabsUnavailable:
            return WriteResult(
                outcome=WriteOutcome.UNKNOWN_WRITE_STATE,
                message=(
                    "The change was sent but could not be read back. It may "
                    "already be live. Check PriceLabs before doing anything "
                    "else — do not retry."
                ),
                stay_date=stay_date,
                old_price=old_price,
            )

        present = after is not None

        if expect_present:
            actual = _price_of(after)

            matched = (
                present
                and expected_price is not None
                and actual is not None
                and round(actual) == round(expected_price)
            )

            if matched:
                return WriteResult(
                        outcome=WriteOutcome.CONFIRMED_APPLIED,
                        message=(
                            "PriceLabs accepted and persisted the override "
                            "(verified by re-reading it). This is not "
                            "confirmation that the channel price changed -- "
                            "that follows on the next PriceLabs refresh."
                        ),
                        stay_date=stay_date,
                        old_price=old_price,
                        new_price=actual,
                    )
        elif not present:
            return WriteResult(
                outcome=WriteOutcome.CONFIRMED_APPLIED,
                message=(
                    "PriceLabs accepted and persisted the removal (verified by "
                    "re-reading). The date is back under dynamic pricing; the "
                    "channel rate follows on the next PriceLabs refresh."
                ),
                stay_date=stay_date,
                old_price=old_price,
            )

        if not acknowledged:
            return WriteResult(
                outcome=WriteOutcome.CONFIRMED_FAILED,
                message="PriceLabs refused the change; nothing was altered.",
                stay_date=stay_date,
                old_price=old_price,
            )

        return WriteResult(
            outcome=WriteOutcome.UNKNOWN_WRITE_STATE,
            message=(
                "PriceLabs accepted the change but the re-read does not match. "
                "Check PriceLabs before doing anything else — do not retry."
            ),
            stay_date=stay_date,
            old_price=old_price,
        )

    def set_override(
        self,
        listing_id: str,
        pms: str,
        stay_date: str,
        price: float,
        currency: str,
        reason: str,
        automation_enabled: bool,
        lead_time_expiry: int = DEFAULT_LEAD_TIME_EXPIRY_DAYS,
    ) -> WriteResult:
        """Set one night's price. Sent once, verified by re-reading."""
        self._guard(automation_enabled)

        before = self._override_for(listing_id, pms, stay_date)

        body = {
            "overrides": [
                {
                    "date": stay_date,
                    "price": str(round(price)),
                    "price_type": "fixed",
                    "currency": currency,
                    "reason": reason[:200],
                    # Hands the date back to dynamic pricing on its own.
                    "lead_time_expiry": lead_time_expiry,
                }
            ],
            "pms": pms,
            "update_children": False,
        }

        try:
            acknowledged = self._send("POST", listing_id, body)

        except PriceLabsUnavailable:
            acknowledged = True  # may have landed; the re-read decides

        return self._verify(
            listing_id,
            pms,
            stay_date,
            expect_present=True,
            expected_price=price,
            old_price=_price_of(before),
            acknowledged=acknowledged,
        )

    def remove_override(
        self,
        listing_id: str,
        pms: str,
        stay_date: str,
        automation_enabled: bool,
    ) -> WriteResult:
        """Return one night to PriceLabs dynamic pricing.

        Removes the override and writes nothing in its place. Replacing a pin
        with another fixed override would leave the date exactly as stuck as it
        was, under a different number.
        """
        self._guard(automation_enabled)

        before = self._override_for(listing_id, pms, stay_date)

        body = {
            "overrides": [{"date": stay_date}],
            "pms": pms,
            "update_children": False,
        }

        try:
            acknowledged = self._send("DELETE", listing_id, body)

        except PriceLabsUnavailable:
            acknowledged = True

        return self._verify(
            listing_id,
            pms,
            stay_date,
            expect_present=False,
            expected_price=None,
            old_price=_price_of(before),
            acknowledged=acknowledged,
        )


def _price_of(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None

    try:
        return float(row.get("price"))

    except (TypeError, ValueError):
        return None
