"""Credential resolution for the PriceLabs connector.

One resolver, read from the environment at call time. The key is never stored
on an instance, logged, audited, or returned, and importing the app never reads
it -- the connector's absence is a configuration state, not a startup failure.
"""

import os

from app.connectors.pricelabs.errors import PriceLabsConfigurationError

API_KEY_ENV_VAR = "PRICELABS_API_KEY"

#: PriceLabs' documented REST base. The MCP endpoint is deliberately not here:
#: its authorization server publishes no registration endpoint, so AgentGuard
#: cannot obtain a client for it. See docs/PRICELABS_API.md.
BASE_URL = "https://api.pricelabs.co"

REQUEST_TIMEOUT_SECONDS = 10.0

USER_AGENT = "AgentGuard/1.0 (PriceLabs connector; read-only)"


def is_configured() -> bool:
    """Whether a PriceLabs credential is present. Never reveals its value."""
    return bool(os.environ.get(API_KEY_ENV_VAR, "").strip())


def resolve_api_key() -> str:
    key = os.environ.get(API_KEY_ENV_VAR, "").strip()

    if not key:
        raise PriceLabsConfigurationError(
            f"{API_KEY_ENV_VAR} is not set; the PriceLabs connector is inactive."
        )

    return key
