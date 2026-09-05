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

Two floors, not one
-------------------
`hard_floor` is the absolute safety line: a LOWER below it is refused, full
stop. `floor_schedule` is the **owner dynamic floor** -- what AgentGuard
considers appropriate for *this* night, given how far out it is and what season
it falls in. They are deliberately different things, and the dynamic floor is
not the PriceLabs permanent MIN: PriceLabs stays configured as it is.

The schedules below were backtested against 12 months of real bookings on
2026-09-05, which is what killed the flat-floor version. A flat $215 on the
2nd-Floor Home would have refused 10 of its 43 bookings (41 nights, $6,953);
the lead-time schedule refuses 5 (30 nights, $5,853) while setting a *higher*
floor far out, where the property actually commands $355. A flat $220 on
Arboretum would have refused 14 of 57 (58 nights, $10,702); the seasonal split
refuses 5 (23 nights, $4,130). Adding lead-time bands inside Arboretum's
seasons made it worse (9 of 57) -- 57 bookings across 10 cells is overfitting,
and the backtest showed it.

That backtest counts only what a floor would have *refused*. It cannot see what
a floor would have won, so it is a worst-case cost, not a net effect, and it
systematically favours lower floors. Read the numbers that way.
"""

import datetime
import os
from dataclasses import dataclass, field

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
#: **Informational only. This is not a permission.** It once served as an
#: alternate unlock for LOWER and RAISE and no longer does: provider-side
#: expiry is unowned, unobservable in the moment, and leaves no per-override
#: audit trail. Even proven, it would tell us the mechanism worked once, not
#: that it worked for a given override on a given day. Explicit cleanup is the
#: real safety mechanism, so `CLEANUP_STRATEGY_VERIFIED` is the sole unlock.
#: A positive result here is a second belt, never the braces.
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

#: Whether the Booking.com guest-discount exposure has been established for the
#: properties that sell there.
#:
#: What is known, measured from 383 real bookings over 12 months (2026-09-05):
#: every one of the seven properties sells through Booking.com, and it is the
#: dominant channel for four of them -- Roslindale takes 43 of its 44 bookings
#: there.
#:
#: What is **not** known: the maximum effective Booking.com guest discount, and
#: whether its components stack. Nothing available to AgentGuard can measure
#: it. PriceLabs reports `discount: 0` on all 383 bookings, carries no
#: commission field, and exposes no channel, promotion or Genius data; there is
#: no Booking.com connector. The owner's extranet shows Genius levels and a
#: dynamic Genius discount, but their combined ceiling has not been established.
#:
#: That uncertainty is by itself enough to fail closed. A LOWER sets a
#: published price, and a published price minus an unknown discount is an
#: unknown guest-facing rate, which cannot be shown to respect any floor. No
#: maximum discount is assumed here, and no floor is divided by one: inventing
#: a number would replace an honest unknown with a confident guess.
#:
#: Independent of CLEANUP_STRATEGY_VERIFIED. Both must pass for a LOWER on a
#: Booking.com property. RAISE is unaffected: it cannot lower a published
#: price, so an unknown discount cannot carry it below a floor.
BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED = False

#: Whether a one-night reservation may ever be recommended.
#:
#: False, as an owner business rule rather than a technical limit: turnover
#: costs roughly $200 per reservation, so a one-night stay can be worth less
#: than it costs to service. AgentGuard must never recommend relaxing a minimum
#: stay to one night, whatever the vacancy or market evidence says.
ONE_NIGHT_STAYS_ALLOWED = False

#: Whether the V2 explicit-cleanup lifecycle has been proven end to end
#: against the live provider.
#:
#: Code passing its tests is not this flag. It flips only after one complete
#: live proof: an approved temporary override, with its cleanup row written
#: first, stored carrying the exact marker, reaching ACTIVE, then at cleanup_at
#: an ownership re-read that matches, exactly one DELETE, and a re-read
#: confirming the override is absent -- CLEANED_UP.
#:
#: 2026-09-05: VERIFIED, on Arboretum Retreat 2026-10-05 -- a night 30 days
#: out, open, with no existing override. One owner-approved RAISE, $250 ->
#: $260 (+4.0%). The cleanup row existed as PENDING_WRITE before the POST;
#: exactly one POST was sent; the confirming re-read matched on marker,
#: byte-for-byte reason, price, `created_at` present and `updated_at ==
#: created_at`, so the row reached ACTIVE. Thirty-one minutes later
#: `app.pricing_cleanup_job` -> `PricingCleanupRunner` proved ownership, sent
#: exactly one DELETE, and re-read the override as absent (9 -> 8 overrides,
#: the other eight untouched) -- CLEANED_UP. A second pass processed 0 and
#: deleted 0, so CLEANED_UP is terminal.
#:
#: The proof is also what found three defects, and this flag records that they
#: are fixed rather than that the first attempt was clean:
#:
#:   * the cleanup row and its audit event carried `approval_id: None` and
#:     `run_id: None`, because `ToolRegistry.execute` passed only schema
#:     arguments. Governance context now travels through `ExecutionContext`,
#:     proven by a production-path integration test rather than by a unit test
#:     that hands the function the value it is meant to obtain elsewhere;
#:   * two processes could select and delete the same row. A due row is now
#:     claimed by an atomic compare-and-swap before the provider is read, and
#:     an expired claim reconciles to NEEDS_REVIEW rather than being taken
#:     over -- elapsed time cannot establish that an external side effect did
#:     not happen;
#:   * a claim holder that stalled past its lease could still call the
#:     provider. `DELETE_STARTED` now fences the irreversible call: it is a
#:     second token-guarded CAS, and `remove_override` runs only if it commits.
#:
#: Every ambiguous state -- UNKNOWN_CLEANUP_STATE, a stranded DELETE_STARTED --
#: is never retried automatically and waits for a person.
#:
#: What this unblocks and what it does not. RAISE is released from *this* gate
#: and remains subject to the deterministic guardrails, per-listing rules,
#: individual human approval, and both runtime write switches. LOWER stays
#: blocked on every Booking.com property by
#: BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED, which is independent and still
#: False. REMOVE_PIN is unaffected. Nothing here enables a write:
#: ENABLE_PRICING_WRITES and the per-listing allowlist are both off.
#:
#: See docs/PRICING_CLEANUP_V2.md.
CLEANUP_STRATEGY_VERIFIED = True

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
class FloorBand:
    """One rule in an owner dynamic-floor schedule.

    A band matches a night when every constraint it sets is satisfied; a
    constraint left as None is not checked. Bands are evaluated in order and
    the first match wins, so a schedule reads top to bottom like the table the
    owner approved.

    `basis` is not decoration. A floor whose evidence is not carried with it
    becomes a number nobody can re-derive or argue with a year from now, which
    is exactly how the flat floors got proposed in the first place.
    """

    floor: float
    basis: str
    #: Calendar months this band applies to. None means any month.
    months: frozenset[int] | None = None
    #: Inclusive lead-time bounds, in days before arrival.
    min_days_out: int | None = None
    max_days_out: int | None = None

    def matches(self, stay_date: datetime.date, days_out: int) -> bool:
        if self.months is not None and stay_date.month not in self.months:
            return False

        if self.min_days_out is not None and days_out < self.min_days_out:
            return False

        return not (self.max_days_out is not None and days_out > self.max_days_out)


#: Arboretum's winter, as the booking history draws it rather than the calendar.
WINTER_MONTHS = frozenset({1, 2, 3})


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
    #: Measured from real bookings, not assumed. True for all seven today.
    sells_on_booking_com: bool = True
    #: Bedroom count AgentGuard analyses with. Set here rather than read from
    #: the provider because the provider can be wrong: PriceLabs records Boston
    #: Bunkers as 3 bedrooms and it has 2, which silently benchmarked it
    #: against homes with an extra bedroom. None means trust the provider.
    bedrooms_override: int | None = None
    #: How this unit competes, when that is not inferable from its numbers.
    positioning: str | None = None
    #: A durable characteristic that legitimately depresses its rate. Recorded
    #: so a future reader does not mistake the gap for an opportunity -- and
    #: deliberately carries no number: Bunkers' realized performance already
    #: reflects it, so a separate adjustment would double-count.
    known_disadvantage: str | None = None
    #: The owner dynamic floor, as an ordered list of bands. Empty means this
    #: property has no schedule yet and `normal_floor` stands in -- an honest
    #: absence rather than a guessed schedule.
    floor_schedule: tuple[FloorBand, ...] = field(default=())


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
        # No lead-time or seasonal shape was established for this unit, so its
        # dynamic floor is flat at the hard floor. PriceLabs MIN $143 /
        # BASE $179, unchanged.
        floor_schedule=(
            FloorBand(
                floor=143.0,
                basis="flat at the hard floor; no lead-time shape established",
            ),
        ),
    ),
    PricingBands(
        listing_id="680434___747413",
        slug="renovated-2nd-floor",
        display_name="Renovated 2nd-Floor Home |",
        # Owner-set 2026-09-05, aligned to the PriceLabs configured minimum.
        # Previously $175 (p10 of the whole 12-month booked-ADR distribution);
        # the lead-time schedule below now carries that work, and the hard
        # floor is back to being purely the safety line.
        hard_floor=170.0,
        normal_floor=215.0,
        auto_raise_ceiling=392.0,
        absolute_ceiling=541.0,
        # PriceLabs MIN $170 / BASE $259, unchanged. The realized ADR declines
        # monotonically toward arrival -- median $355 / $271 / $254 / $200 --
        # so the band structure *is* this property's existing strategy, and a
        # flat floor fights it. n=43 bookings over 12 months.
        floor_schedule=(
            FloorBand(
                floor=240.0,
                basis="p10 of realized ADR 30+ days out (n=16, median $355)",
                min_days_out=30,
            ),
            FloorBand(
                floor=175.0,
                basis="p10 of realized ADR 15-29 days out (n=10, median $271)",
                min_days_out=15,
                max_days_out=29,
            ),
            FloorBand(
                floor=170.0,
                basis=(
                    "hard floor; the band's own p10 is $168 (n=3), below it"
                ),
                min_days_out=8,
                max_days_out=14,
            ),
            FloorBand(
                floor=170.0,
                basis=(
                    "hard floor; the band's p10 is $197 but on n=3, too thin "
                    "to lift a floor above the safety line"
                ),
                min_days_out=4,
                max_days_out=7,
            ),
            FloorBand(
                floor=170.0,
                basis=(
                    "hard floor binds; last-minute is bimodal (n=11, p10 $132, "
                    "median $226, p75 $276)"
                ),
                max_days_out=3,
            ),
        ),
    ),
    PricingBands(
        listing_id="680444___747423",
        slug="boston-bunkers",
        display_name="Boston Bunkers",
        hard_floor=143.0,
        normal_floor=143.0,
        auto_raise_ceiling=252.0,
        absolute_ceiling=324.0,
        # A 2-bedroom lower-level unit, not a 3-bedroom peer of the others in
        # its building. Held at MIN $143 / BASE $179 on its own evidence:
        # realized p25 ADR $142, median $180, PriceLabs' own recommended base
        # $182, 67% occupancy against a 38% 2-bedroom market, 4.86-night mean
        # stay, and zero cancellations in 44 reservations. The noise
        # disadvantage is already inside those numbers.
        bedrooms_override=2,
        positioning="budget/lower-level",
        known_disadvantage="noise",
        # Flat at the hard floor. PriceLabs MIN $143 / BASE $179, unchanged,
        # and the PriceLabs bedroom field is deliberately left at 3 -- only
        # AgentGuard's analytical comp set moved to 2-bedroom.
        floor_schedule=(
            FloorBand(
                floor=143.0,
                basis="flat at the hard floor; budget/lower-level positioning",
            ),
        ),
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
        # PriceLabs MIN $189 / BASE $268, unchanged. This property's low prices
        # are seasonal, not last-minute: winter median $215 against Apr-Dec
        # $322. Deliberately *no* lead-time bands -- adding them backtested
        # worse (9 refusals against 5), because 57 bookings do not support 10
        # cells.
        floor_schedule=(
            FloorBand(
                floor=189.0,
                basis=(
                    "hard floor for now; winter evidence is thin (n=10, none "
                    "in January) and its p10 of $150 sits below the floor"
                ),
                months=WINTER_MONTHS,
            ),
            FloorBand(
                floor=205.0,
                basis="p10 of realized ADR Apr-Dec (n=47, median $322)",
            ),
        ),
    ),
)

BANDS_BY_LISTING: dict[str, PricingBands] = {b.listing_id: b for b in BANDS}


def dynamic_floor(
    bands: PricingBands | None,
    stay_date: datetime.date,
    days_out: int,
) -> tuple[float, str] | None:
    """The owner dynamic floor for one night, with the evidence behind it.

    None when the listing has no bands or no schedule: three of the seven
    properties have not been analysed for a lead-time or seasonal shape, and
    inventing one for them would be exactly the guess this whole exercise
    rejected. Callers fall back to `normal_floor`.

    Never returns below the hard floor. The dynamic floor is what AgentGuard
    considers appropriate *today*; the hard floor is what it will not go under
    on any day, so the schedule can only ever raise the line, never lower it.
    """
    if bands is None or not bands.floor_schedule:
        return None

    for band in bands.floor_schedule:
        if band.matches(stay_date, days_out):
            if band.floor >= bands.hard_floor:
                return band.floor, band.basis

            return bands.hard_floor, f"hard floor ({band.basis})"

    return None


def unverified_reason(action: str, listing_id: str | None = None) -> str | None:
    """Why this action is blocked pending live verification, or None.

    Keyed on the action, and for LOWER on the listing too: the write paths rest
    on different unproven things and will be unblocked at different times and,
    for channel exposure, possibly property by property.
    """
    if (
        action == "LOWER"
        and not BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED
        and _sells_on_booking_com(listing_id)
    ):
        return (
            "Lowering a price is blocked for this property: it sells through "
            "Booking.com, and the maximum effective guest discount there and "
            "whether its components stack are currently unknown. A published "
            "price minus an unknown discount is an unknown guest-facing rate, "
            "which cannot be shown to respect any floor."
        )

    if action in {"LOWER", "RAISE"} and not CLEANUP_STRATEGY_VERIFIED:
        return (
            "A fixed-price write is blocked: AgentGuard's explicit cleanup "
            "lifecycle has not completed a full live proof, so this write has "
            "no expiry it owns and could strand a permanent pin."
        )

    if action == "REMOVE_PIN" and not DELETE_ENDPOINT_VERIFIED:
        return (
            "REMOVE_PIN is blocked: the DELETE overrides endpoint has not been "
            "live-verified through the approval -> one write -> re-read path."
        )

    return None


def _sells_on_booking_com(listing_id: str | None) -> bool:
    """Whether this listing is exposed to Booking.com discounting.

    An unknown listing is treated as exposed. Absence of evidence is not
    evidence of absence, and the safe direction is to withhold the write.
    """
    if listing_id is None:
        return True

    band = BANDS_BY_LISTING.get(listing_id)

    return True if band is None else band.sells_on_booking_com


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
