"""Property configuration and credential resolution for the Lodgify connector.

The property table is a closed allowlist copied from the audited Priyanka Homes
repository (`data/properties.json`). It is configuration, not data: no schema
change and no runtime dependency on that repository.

The model never supplies a Lodgify property id. It names a slug; the connector
resolves the numeric identifiers. That is what keeps a hallucinated or
attacker-supplied id from reaching the provider.
"""

import os
from dataclasses import dataclass

from app.connectors.lodgify.errors import LodgifyConfigurationError

API_KEY_ENV_VAR = "LODGIFY_API_KEY"

BASE_URL = "https://api.lodgify.com"

REQUEST_TIMEOUT_SECONDS = 8.0

USER_AGENT = "AgentGuard/1.0 (Lodgify connector; read-only)"

# Availability is a live, per-night calendar; a wide window is a large response
# and a slow call for no operator benefit. 31 days covers "this month" and
# "these dates" questions, which is what the tools exist to answer.
MAX_AVAILABILITY_DAYS = 31

MIN_GUESTS = 1

MAX_GUESTS = 32


@dataclass(frozen=True)
class PropertyConfig:
    """One bookable property. Provider ids stay server-side."""

    slug: str
    display_name: str
    lodgify_property_id: int
    room_type_id: int


@dataclass(frozen=True)
class StandaloneProperty:
    """A property deliberately outside Lodgify.

    Listed so an operator sees the full portfolio, but it carries no provider
    identifiers, so it structurally cannot be queried through Lodgify.
    """

    slug: str
    display_name: str


# Verified 2026-08-19 against the live Lodgify account and recorded in the
# Priyanka Homes repo (docs/LODGIFY_API.md section 9). Do not edit by hand
# without re-verifying against that source.
LODGIFY_PROPERTIES: tuple[PropertyConfig, ...] = (
    PropertyConfig(
        slug="renovated-3rd-floor-retreat-3-beds-roslindale-village",
        display_name="Renovated 3rd-Floor Retreat | 3 Beds | Roslindale Village",
        lodgify_property_id=680420,
        room_type_id=747399,
    ),
    PropertyConfig(
        slug="renovated-2nd-floor-home",
        display_name="Renovated 2nd-Floor Home",
        lodgify_property_id=680434,
        room_type_id=747413,
    ),
    PropertyConfig(
        slug="budget-friendly-basement-2br-retreat",
        display_name="Budget-Friendly Basement 2BR Retreat",
        lodgify_property_id=680444,
        room_type_id=747423,
    ),
    PropertyConfig(
        slug="modern-condo-walk-out-basement-near-train",
        display_name="Modern Condo | Walk-Out Basement | Near Train",
        lodgify_property_id=680447,
        room_type_id=747426,
    ),
    PropertyConfig(
        slug="boston-hospitality-homes-harvard",
        display_name="Boston Hospitality Homes, Allston",
        lodgify_property_id=681286,
        room_type_id=748333,
    ),
    PropertyConfig(
        slug="boston-condo-second-floor",
        display_name="Boston condo second Floor",
        lodgify_property_id=681293,
        room_type_id=748340,
    ),
    PropertyConfig(
        slug="arboretum-retreat-city-of-boston",
        display_name="“Arboretum Retreat” city of Boston",
        lodgify_property_id=681301,
        room_type_id=748348,
    ),
)

# Intentionally not on Lodgify: inquiry-only, no property id exists upstream.
STANDALONE_PROPERTIES: tuple[StandaloneProperty, ...] = (
    StandaloneProperty(
        slug="south-boston-seaside-residence",
        display_name="South Boston Seaside Residence",
    ),
)


LODGIFY_SLUGS: tuple[str, ...] = tuple(p.slug for p in LODGIFY_PROPERTIES)


def find_lodgify_property(slug: str) -> PropertyConfig:
    """Resolve a slug to its provider identifiers.

    Raises:
        ValueError: If the slug is unknown or names a non-Lodgify property.
            A ValueError is recoverable in the agent loop, so the model is told
            what is valid and can correct itself.
    """
    for prop in LODGIFY_PROPERTIES:
        if prop.slug == slug:
            return prop

    for standalone in STANDALONE_PROPERTIES:
        if standalone.slug == slug:
            raise ValueError(
                f"{slug!r} is not booked through Lodgify, so live availability "
                f"and pricing are not available for it."
            )

    raise ValueError(
        f"Unknown property: {slug!r}. Valid properties: {', '.join(LODGIFY_SLUGS)}."
    )


def resolve_api_key() -> str:
    """Read the connector credential from the environment.

    The single seam for credential resolution: a future Secrets Manager backend
    replaces this function body and nothing else. There is deliberately no
    fallback value -- an unset key makes the connector unavailable rather than
    silently producing fabricated results.
    """
    key = os.environ.get(API_KEY_ENV_VAR)

    if not key:
        raise LodgifyConfigurationError(
            f"{API_KEY_ENV_VAR} is not set; the Lodgify connector is unavailable."
        )

    return key


def is_configured() -> bool:
    """Whether a credential is available, without reading its value."""
    return bool(os.environ.get(API_KEY_ENV_VAR))
