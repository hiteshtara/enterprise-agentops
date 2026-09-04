"""Owner-approved pricing bands and automation switches.

**Nothing here is self-service.** Every band below was derived from evidence
(PriceLabs configured minimum, the property's own booked-ADR distribution, and
the neighbourhood comp set for the actual stay dates) and is a *proposal until
the owner signs it off*. `pricing_automation_enabled` is False for all seven
listings, and the global `ENABLE_PRICING_WRITES` defaults False, so no band
here can move a price until two separate switches are turned on and a human
approves the individual change.

Derivation, recorded so the numbers can be re-checked rather than trusted:

  HARD FLOOR      max(PriceLabs configured minimum, p10 of 12-month booked ADR)
  NORMAL FLOOR    p25 of 12-month booked ADR, never below the hard floor
  AUTO-RAISE      min(median market p75 over the horizon,
                      this property's own p90 booked ADR)
  ABSOLUTE        the most this property has actually achieved

The second term of AUTO-RAISE is load-bearing. Harvard's comp set (a 5-bedroom
band) has a market p90 of $1,544, which the property has never come close to
achieving; capping at its own p90 booked ADR holds it to $831 instead. A
ceiling taken from the market alone would let automation chase a number no
guest has ever paid here.
"""

import os
from dataclasses import dataclass

#: Global kill switch. Nothing writes to PriceLabs unless this is explicitly
#: "true". Absent, blank, or any other value means off.
WRITES_ENV_VAR = "ENABLE_PRICING_WRITES"

#: Per-listing switch, independent of the global one. A comma-separated list of
#: listing ids or slugs. Empty means every listing is off, which is the default
#: and the only safe default: enabling automation is an act, never an omission.
#: Kept in the environment rather than in this file so turning one listing on
#: for an experiment is a deployment decision, not a code change.
AUTOMATION_ENV_VAR = "PRICELABS_AUTOMATION_ENABLED"

#: The largest single move automation may propose, as a fraction of the
#: current price. Deliberately *below* the owner's own median manual move of
#: 17% (measured across 109 logged manual changes), so an automated step is
#: never larger than a step he takes by hand. A larger desired move is reached
#: by re-evaluating against fresh market state, never by chaining executions.
MAX_CHANGE_PER_RUN = 0.10

# -- verification gates ----------------------------------------------------
#
# These record what has actually been proven against the live provider, and
# they gate the write paths that depend on it. They are flags rather than prose
# because an unverified assumption that only lives in a comment is one nobody
# is stopped by.

#: Whether `lead_time_expiry` has been *empirically* shown to expire an
#: override.
#:
#: 2026-09-04: the first live write (Modern Condo, 2026-09-21, $239 -> $246)
#: sent `lead_time_expiry: 3`. PriceLabs accepted it and echoed it back
#: unchanged on re-read, which proves acceptance and persistence and nothing
#: more. No computed expiry date, no `expires_at`, and no status field is
#: returned, so there is no way to confirm from the API that the override will
#: actually lapse three days before arrival.
#:
#: To settle it: re-read that override on or after 2026-09-18. Gone means the
#: semantics hold. Still present means a price-setting write strands a
#: permanent pin -- the exact failure this feature exists to surface.
#:
#: While False, no fixed-price write (LOWER or RAISE) may execute.
EXPIRY_SEMANTICS_VERIFIED = False

#: Whether `DELETE /v1/listings/{id}/overrides` has been live-verified through
#: the same approval -> one write -> re-read path the POST went through.
#:
#: 2026-09-04: VERIFIED. A controlled removal ran through the approval -> one
#: write -> re-read path on Roslindale 2026-09-13, a night that was already
#: booked so the removal could not affect any guest. One DELETE was sent, the
#: override was absent on re-read, the listing's override count went 18 -> 17,
#: and the five surrounding pinned dates were untouched. The booking itself was
#: unchanged (status Booked, ADR 93.0, booked_date 2026-08-31).
#:
#: This unblocks REMOVE_PIN only. It says nothing about the fixed-price
#: lifecycle, which is what EXPIRY_SEMANTICS_VERIFIED still gates.
DELETE_ENDPOINT_VERIFIED = True


@dataclass(frozen=True)
class PricingBands:
    """One property's owner-approved limits."""

    listing_id: str
    slug: str
    display_name: str
    hard_floor: float
    normal_floor: float
    auto_raise_ceiling: float
    absolute_ceiling: float
    #: Per-listing switch, independent of the global one. Both must be on.
    automation_enabled: bool = False
    #: When True, a RAISE on this listing always needs a human decision,
    #: whatever the confidence. Set for Harvard until its comp set is validated.
    raise_requires_human: bool = False


#: Derived 2026-09-04 from live PriceLabs data. Proposals pending owner sign-off.
BANDS: tuple[PricingBands, ...] = (
    PricingBands(
        listing_id="680420___747399",
        slug="roslindale-3rd-floor",
        display_name="Renovated 3rd-Floor Retreat | 3 Beds | Roslindale Village",
        hard_floor=143.0,
        normal_floor=143.0,
        auto_raise_ceiling=222.0,
        absolute_ceiling=291.0,
    ),
    PricingBands(
        listing_id="680434___747413",
        slug="renovated-2nd-floor",
        display_name="Renovated 2nd-Floor Home |",
        hard_floor=175.0,
        normal_floor=215.0,
        auto_raise_ceiling=392.0,
        absolute_ceiling=541.0,
    ),
    PricingBands(
        listing_id="680444___747423",
        slug="boston-bunkers",
        display_name="Boston Bunkers",
        hard_floor=143.0,
        normal_floor=143.0,
        auto_raise_ceiling=252.0,
        absolute_ceiling=324.0,
    ),
    PricingBands(
        listing_id="680447___747426",
        slug="modern-condo",
        display_name="Modern Condo | Walk-Out Basement | Near Train",
        hard_floor=199.0,
        normal_floor=219.0,
        auto_raise_ceiling=424.0,
        absolute_ceiling=662.0,
    ),
    PricingBands(
        listing_id="681286___748333",
        slug="harvard",
        display_name="Boston Hospitality Homes, Harvard",
        hard_floor=338.0,
        normal_floor=360.0,
        auto_raise_ceiling=831.0,
        absolute_ceiling=1111.0,
        # Its comp set is a 5-bedroom band whose p90 is $1,544 -- a number this
        # property has never achieved. Until a narrower comp set is validated,
        # no RAISE here is automatic at any confidence.
        raise_requires_human=True,
    ),
    PricingBands(
        listing_id="681293___748340",
        slug="boston-condo-second-floor",
        display_name="Boston condo second Floor",
        hard_floor=189.0,
        normal_floor=221.0,
        auto_raise_ceiling=424.0,
        absolute_ceiling=637.0,
    ),
    PricingBands(
        listing_id="681301___748348",
        slug="arboretum",
        display_name="Arboretum Retreat city of Boston",
        hard_floor=189.0,
        normal_floor=221.0,
        auto_raise_ceiling=442.0,
        absolute_ceiling=748.0,
    ),
)

BANDS_BY_LISTING: dict[str, PricingBands] = {b.listing_id: b for b in BANDS}


def unverified_reason(action: str) -> str | None:
    """Why this action is blocked pending live verification, or None.

    Keyed on the action rather than a single global flag: the two write paths
    rest on different unproven assumptions and will be unblocked at different
    times.
    """
    if action in {"LOWER", "RAISE"} and not EXPIRY_SEMANTICS_VERIFIED:
        return (
            "A fixed-price write is blocked: lead_time_expiry has been "
            "accepted and stored by PriceLabs but its expiration behaviour has "
            "not been empirically verified, so this write could strand a "
            "permanent pin. Re-read the 2026-09-21 override on or after "
            "2026-09-18 to settle it."
        )

    if action == "REMOVE_PIN" and not DELETE_ENDPOINT_VERIFIED:
        return (
            "REMOVE_PIN is blocked: the DELETE overrides endpoint has not been "
            "live-verified through the approval -> one write -> re-read path."
        )

    return None


def automation_allowlist() -> frozenset[str]:
    """Listing ids or slugs whose automation is switched on."""
    raw = os.environ.get(AUTOMATION_ENV_VAR, "")

    return frozenset(
        token.strip() for token in raw.split(",") if token.strip()
    )


def writes_enabled() -> bool:
    """The global kill switch. Off unless explicitly, exactly enabled."""
    return os.environ.get(WRITES_ENV_VAR, "").strip().lower() == "true"


def bands_for(listing_id: str) -> PricingBands | None:
    """Owner bands for a listing, or None when it has none.

    A listing with no bands has no limits to check a price against, so it can
    never be written to. Absence is a refusal, not a default.

    `automation_enabled` is resolved from the environment on every call rather
    than baked into the table, so a listing can be switched on for one
    controlled exercise and switched off again without editing code.
    """
    band = BANDS_BY_LISTING.get(listing_id)

    if band is None:
        return None

    allowed = automation_allowlist()

    enabled = band.listing_id in allowed or band.slug in allowed

    if enabled == band.automation_enabled:
        return band

    return PricingBands(
        **{**band.__dict__, "automation_enabled": enabled},
    )
