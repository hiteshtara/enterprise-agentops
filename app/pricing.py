"""Model pricing, in one place.

Prices change. Keeping them in a single module with an explicit "as of" date
means a stale figure is visible and correctable, rather than scattered through
arithmetic at call sites.

An unknown model has no price. It never falls back to another model's rate and
never becomes zero -- a cost of $0.00 would read as "this was free", which is a
different and false claim from "we do not know what this cost".
"""

from dataclasses import dataclass

from app.protocol import ModelUsage

PRICES_AS_OF = "2026-09"

USD_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens."""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None


class PricingRegistry:
    """Looks up pricing by provider and model."""

    def __init__(
        self, prices: dict[tuple[str, str], ModelPricing] | None = None
    ) -> None:
        self._prices = dict(prices if prices is not None else DEFAULT_PRICES)

    def get(self, provider: str, model: str | None) -> ModelPricing | None:
        if model is None:
            return None

        return self._prices.get((provider.lower(), model.lower()))

    def estimate(
        self,
        provider: str,
        model: str | None,
        usage: ModelUsage | None,
    ) -> float | None:
        """Estimated USD for one model call, or None when it cannot be known.

        Returns None -- never 0.0 -- when the model is unpriced or the provider
        reported no token counts.
        """
        pricing = self.get(provider, model)

        if pricing is None or usage is None:
            return None

        if usage.input_tokens is None and usage.output_tokens is None:
            return None

        billable_input = usage.input_tokens or 0
        cached = usage.cached_input_tokens or 0

        cost = 0.0

        if pricing.cached_input_per_million is not None and cached:
            # Cached input is billed at its own rate; the remainder at full rate.
            uncached = max(0, billable_input - cached)

            cost += uncached * pricing.input_per_million / USD_PER_MILLION
            cost += cached * pricing.cached_input_per_million / USD_PER_MILLION
        else:
            cost += billable_input * pricing.input_per_million / USD_PER_MILLION

        cost += (
            (usage.output_tokens or 0) * pricing.output_per_million / USD_PER_MILLION
        )

        return round(cost, 6)


GPT_5_4_MINI = ModelPricing(
    input_per_million=0.25,
    output_per_million=2.00,
    cached_input_per_million=0.025,
)

# Published list prices in USD per million tokens, recorded manually. Nothing
# here is fetched at runtime.
#
# Dated snapshots are listed explicitly, one entry each. The API reports the
# snapshot it actually served (e.g. "gpt-5.4-mini-2026-03-17"), and pricing a
# snapshot as its alias is a decision recorded here rather than inferred by
# prefix matching -- a prefix rule would happily misprice an unrelated model
# that merely shares a name stem.
DEFAULT_PRICES: dict[tuple[str, str], ModelPricing] = {
    ("openai", "gpt-5.4-mini"): GPT_5_4_MINI,
    ("openai", "gpt-5.4-mini-2026-03-17"): GPT_5_4_MINI,
}


default_pricing_registry = PricingRegistry()
