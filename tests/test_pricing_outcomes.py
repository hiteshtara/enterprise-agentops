"""Observational outcome tracking for executed pricing actions.

Two things are being defended here, and they are not the same thing.

*Correctness*: hours are hours, check-out is exclusive, a reservation booked
before the action is not an outcome of it.

*Honesty*: unknown never becomes `False`, first-booking evidence survives a
cancellation, finalization is revocable, and nothing anywhere claims a price
change caused a booking.

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.pricing_outcomes import (
    FINALIZATION_DAYS_AFTER_CHECKOUT,
    FINALIZATION_MIN_PASSES,
    BookingStatus,
    PricingOutcomeStore,
    hours_between,
    lead_days,
    to_payload,
)
from app.pricing_reconciler import (
    PricingOutcomeReconciler,
    covers,
    occupied_nights,
    qualifies,
)

BUNKERS = "680444___747423"

STAY = "2026-09-10"

#: The action. Every booking below is placed relative to this instant.
ACTED = "2026-09-06T10:00:00+00:00"

NOW = datetime.datetime(2026, 9, 6, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def store(database) -> PricingOutcomeStore:
    return PricingOutcomeStore(database=database)


def executed(store, **over):
    fields = {
        "approval_id": "ap-1",
        "run_id": "run-1",
        "cleanup_id": "cl-1",
        "listing_id": BUNKERS,
        "stay_date": STAY,
        "action": "LOWER",
        "executed_at": ACTED,
        "write_outcome": "CONFIRMED_APPLIED",
        "price_before": 182.0,
        "price_after": 164.0,
        "currency": "USD",
        "days_out": 4,
        "evidence": {
            "history_adr": 149.5,
            "history_sample_count": 9,
            "market_p25": 199.8,
            "demand": "Normal Demand",
            "market_signal_conflict": True,
            "hard_floor": 143.0,
            "owner_floor": 143.0,
            "observed_commission_rate": 23.0,
        },
    }

    fields.update(over)

    return store.record_execution(**fields)


def reservation(**over):
    """One provider reservation, in the connector's minimal shape."""
    row = {
        "reservation_id": "r-1",
        "listing_id": BUNKERS,
        "booked_date": "2026-09-06T16:30:00.000Z",
        "check_in": "2026-09-10",
        "check_out": "2026-09-12",
        "no_of_days": 2,
        "rental_revenue": 340.0,
        "booking_status": "booked",
        "booking_channel": "bcom",
    }

    row.update(over)

    return row


class FakeReader:
    """Returns canned reservations. Has no writer of any kind."""

    def __init__(self, rows=None, raises=None):
        self.rows = rows or []
        self.raises = raises
        self.calls = []

    def reservations(self, listing_id, pms, start_date, end_date):
        self.calls.append((listing_id, pms, start_date, end_date))

        if self.raises:
            raise self.raises

        return self.rows


def reconciler(store, reader):
    return PricingOutcomeReconciler(reader=reader, outcomes=store)


# -- hours, not days ------------------------------------------------------


def test_a_booking_three_hours_after_the_action_records_three_hours(store):
    record = executed(store)
    reader = FakeReader([reservation(booked_date="2026-09-06T13:00:00.000Z")])

    reconciler(store, reader).run_once(now=NOW)

    assert store.get(record.id).first_hours_from_action == pytest.approx(3.0)


def test_a_booking_twenty_three_hours_later_is_not_rounded_to_a_day(store):
    """The whole reason this column is hours.

    A same-calendar-day booking and a next-morning one are different facts,
    and `0 days` erases the difference.
    """
    record = executed(store)
    reader = FakeReader([reservation(booked_date="2026-09-07T09:00:00.000Z")])

    reconciler(store, reader).run_once(now=NOW + datetime.timedelta(days=1))

    assert store.get(record.id).first_hours_from_action == pytest.approx(23.0)


def test_the_worked_example_from_the_specification(store):
    """LOWER at 10:00, booking at 16:30 -> 6.5 hours, never '0 days'."""
    record = executed(store)
    reader = FakeReader([reservation(booked_date="2026-09-06T16:30:00.000Z")])

    reconciler(store, reader).run_once(now=NOW)

    assert store.get(record.id).first_hours_from_action == pytest.approx(6.5)


def test_lead_days_stays_date_based(store):
    """Check-in has date semantics; hours there would imply false precision."""
    record = executed(store)
    reader = FakeReader([reservation()])

    reconciler(store, reader).run_once(now=NOW)

    assert store.get(record.id).first_booking_lead_days == 4


# -- the load-bearing rule ------------------------------------------------


def test_a_reservation_booked_one_second_before_the_action_is_excluded(store):
    """It covers the night, it is booked, and it is still not an outcome.

    The night was already sold when the price changed. Counting it would
    manufacture a success rate out of nights that were never available.
    """
    record = executed(store)
    reader = FakeReader(
        [reservation(booked_date="2026-09-06T09:59:59.000Z")]
    )

    reconciler(store, reader).run_once(now=NOW)

    assert store.get(record.id).first_booked_at is None


def test_a_reservation_booked_one_second_after_the_action_qualifies(store):
    record = executed(store)
    reader = FakeReader(
        [reservation(booked_date="2026-09-06T10:00:01.000Z")]
    )

    reconciler(store, reader).run_once(now=NOW)

    assert store.get(record.id).first_booked_at == "2026-09-06T10:00:01.000Z"


def test_a_booking_exactly_at_the_action_instant_does_not_qualify(store):
    """The boundary is strict. Simultaneous is not 'after'."""
    assert not qualifies(
        reservation(booked_date=ACTED),
        STAY,
        ACTED,
    )


# -- night expansion ------------------------------------------------------


def test_check_out_is_exclusive():
    row = reservation(check_in="2026-09-10", check_out="2026-09-12")

    assert occupied_nights(row) == (
        datetime.date(2026, 9, 10),
        datetime.date(2026, 9, 11),
    )
    assert covers(row, "2026-09-11")
    assert not covers(row, "2026-09-12"), "check-out night is not occupied"


def test_a_multi_night_reservation_covers_a_night_after_check_in(store):
    """Arrival on the 8th, our night is the 10th. `check_in` alone misses it."""
    record = executed(store)
    reader = FakeReader(
        [
            reservation(
                check_in="2026-09-08",
                check_out="2026-09-13",
                no_of_days=5,
                rental_revenue=900.0,
                booked_date="2026-09-06T18:00:00.000Z",
            )
        ]
    )

    reconciler(store, reader).run_once(now=NOW)

    stored = store.get(record.id)

    assert stored.first_booked_at == "2026-09-06T18:00:00.000Z"
    assert stored.current_booking_status == BookingStatus.BOOKED
    # 900 / 5 -- a stay average, never this night's rate.
    assert stored.first_realized_stay_adr == pytest.approx(180.0)


def test_a_reservation_that_ends_on_our_night_does_not_cover_it(store):
    record = executed(store)
    reader = FakeReader(
        [reservation(check_in="2026-09-08", check_out="2026-09-10", no_of_days=2)]
    )

    reconciler(store, reader).run_once(now=NOW)

    stored = store.get(record.id)

    assert stored.first_booked_at is None
    assert stored.current_booking_status == BookingStatus.NONE


# -- write-once -----------------------------------------------------------


def test_first_evidence_cannot_be_overwritten_through_the_store(store):
    """The guard is the WHERE clause, so a wrong caller still cannot win."""
    from app.pricing_outcomes import BookingObservation

    record = executed(store)

    first = BookingObservation(
        reservation_id="r-1",
        booked_at="2026-09-06T16:30:00.000Z",
        status="booked",
        realized_stay_adr=170.0,
        channel="bcom",
        check_out="2026-09-12",
    )

    later = BookingObservation(
        reservation_id="r-2",
        booked_at="2026-09-08T08:00:00.000Z",
        status="booked",
        realized_stay_adr=200.0,
        channel="airbnb",
        check_out="2026-09-12",
    )

    assert store.note_first_booking(
        record.id, first, stay_date=STAY, executed_at=ACTED
    )

    # Asked directly of the store, deliberately bypassing every caller-side
    # check, because caller discipline is exactly what is not being trusted.
    assert not store.note_first_booking(
        record.id, later, stay_date=STAY, executed_at=ACTED
    )

    stored = store.get(record.id)

    assert stored.first_reservation_id == "r-1"
    assert stored.first_booked_at == "2026-09-06T16:30:00.000Z"
    assert stored.first_booking_channel == "bcom"
    assert stored.first_realized_stay_adr == pytest.approx(170.0)


# -- cancellation ---------------------------------------------------------


def test_a_cancellation_preserves_first_evidence_byte_for_byte(store):
    """'Never booked' and 'booked then cancelled' must stay distinguishable."""
    record = executed(store)
    booked = reservation(booked_date="2026-09-06T16:30:00.000Z")

    reconciler(store, FakeReader([booked])).run_once(now=NOW)

    before = {
        k: v
        for k, v in vars(store.get(record.id)).items()
        if k.startswith("first_")
    }

    cancelled = dict(booked, booking_status="cancelled")
    later = NOW + datetime.timedelta(days=1)

    reconciler(store, FakeReader([cancelled])).run_once(now=later)

    stored = store.get(record.id)
    after = {k: v for k, v in vars(stored).items() if k.startswith("first_")}

    assert after == before, "first_* must survive a cancellation untouched"
    assert stored.current_booking_status == BookingStatus.CANCELLED
    assert stored.cancelled_after_booking is True
    assert stored.cancellation_first_observed_at is not None


def test_the_cancellation_timestamp_is_ours_and_is_recorded_once(store):
    """The provider's `cancelled_on` is a sentinel, so this is an observation.

    It must record when we *first* noticed, not the most recent pass that saw
    the same cancellation.
    """
    record = executed(store)
    booked = reservation(booked_date="2026-09-06T16:30:00.000Z")

    reconciler(store, FakeReader([booked])).run_once(now=NOW)

    cancelled = dict(booked, booking_status="cancelled")
    first_seen = NOW + datetime.timedelta(days=1)

    reconciler(store, FakeReader([cancelled])).run_once(now=first_seen)
    observed = store.get(record.id).cancellation_first_observed_at

    reconciler(store, FakeReader([cancelled])).run_once(
        now=NOW + datetime.timedelta(days=3)
    )

    assert store.get(record.id).cancellation_first_observed_at == observed


def test_the_field_is_not_called_cancelled_at():
    """Naming a poll timestamp `cancelled_at` would launder an observation."""
    from app.db_models import PricingActionOutcomeRecord

    columns = set(PricingActionOutcomeRecord.__table__.columns.keys())

    assert "cancellation_first_observed_at" in columns
    assert "cancelled_at" not in columns
    assert "cancelled_on" not in columns


# -- rebooking ------------------------------------------------------------


def test_a_rebooking_moves_current_and_freezes_first(store):
    """The four-way distinction the schema exists to carry."""
    record = executed(store)
    first = reservation(booked_date="2026-09-06T16:30:00.000Z")

    reconciler(store, FakeReader([first])).run_once(now=NOW)

    cancelled = dict(first, booking_status="cancelled")

    reconciler(store, FakeReader([cancelled])).run_once(
        now=NOW + datetime.timedelta(days=1)
    )

    second = reservation(
        reservation_id="r-2",
        booked_date="2026-09-08T08:00:00.000Z",
        booking_channel="airbnb",
        rental_revenue=400.0,
    )

    reconciler(store, FakeReader([cancelled, second])).run_once(
        now=NOW + datetime.timedelta(days=2)
    )

    stored = store.get(record.id)

    assert stored.first_reservation_id == "r-1"
    assert stored.first_booking_channel == "bcom"
    assert stored.current_reservation_id == "r-2"
    assert stored.current_booking_channel == "airbnb"
    assert stored.current_booking_status == BookingStatus.BOOKED
    # Still true: the booking made after the action did cancel.
    assert stored.cancelled_after_booking is True


# -- unknown stays unknown ------------------------------------------------


def test_a_provider_failure_leaves_the_outcome_unknown(store):
    """Not `False`, not `none`, not zero. Untouched."""
    from app.connectors.pricelabs.errors import PriceLabsUnavailable

    record = executed(store)
    reader = FakeReader(raises=PriceLabsUnavailable("down"))

    results = reconciler(store, reader).run_once(now=NOW)

    stored = store.get(record.id)

    assert stored.current_booking_status is None
    assert stored.cancelled_after_booking is None
    assert stored.first_booked_at is None
    assert stored.last_reconciled_at is None, "a failed read is not a pass"
    assert stored.reconcile_pass_count == 0
    assert results[0].status is None


def test_a_provider_failure_does_not_erase_what_was_already_known(store):
    from app.connectors.pricelabs.errors import PriceLabsUnavailable

    record = executed(store)
    booked = reservation(booked_date="2026-09-06T16:30:00.000Z")

    reconciler(store, FakeReader([booked])).run_once(now=NOW)

    reconciler(
        store, FakeReader(raises=PriceLabsUnavailable("down"))
    ).run_once(now=NOW + datetime.timedelta(days=1))

    stored = store.get(record.id)

    assert stored.first_booked_at == "2026-09-06T16:30:00.000Z"
    assert stored.current_booking_status == BookingStatus.BOOKED


def test_unknown_helpers_return_none_rather_than_zero():
    assert hours_between(None, ACTED) is None
    assert hours_between(ACTED, "not a timestamp") is None
    assert lead_days(STAY, None) is None
    assert lead_days("nonsense", ACTED) is None


# -- finalization ---------------------------------------------------------


def test_a_row_is_not_finalized_on_elapsed_time_alone(store):
    """Passes are a separate condition from the clock."""
    record = executed(store)
    reader = FakeReader([])

    long_after = NOW + datetime.timedelta(
        days=FINALIZATION_DAYS_AFTER_CHECKOUT + 60
    )

    reconciler(store, reader).run_once(now=long_after)

    assert store.get(record.id).reconcile_pass_count < FINALIZATION_MIN_PASSES
    assert store.get(record.id).finalized_at is None


def test_a_settled_row_finalizes_after_the_policy_window(store):
    record = executed(store)
    reader = FakeReader([])

    moment = NOW

    for _ in range(FINALIZATION_MIN_PASSES + 1):
        moment = moment + datetime.timedelta(
            days=FINALIZATION_DAYS_AFTER_CHECKOUT + 10
        )
        reconciler(store, reader).run_once(now=moment)

    assert store.get(record.id).finalized_at is not None


def test_a_finalized_row_can_be_reopened(store):
    """Finalized means polling stopped, never that the row became immutable."""
    record = executed(store)
    reader = FakeReader([])

    moment = NOW

    for _ in range(FINALIZATION_MIN_PASSES + 1):
        moment = moment + datetime.timedelta(
            days=FINALIZATION_DAYS_AFTER_CHECKOUT + 10
        )
        reconciler(store, reader).run_once(now=moment)

    assert store.get(record.id).finalized_at is not None

    late = reservation(booked_date="2026-09-06T16:30:00.000Z")

    reopened = reconciler(store, reader).reopen_if_changed(
        store.get(record.id),
        [late],
        now=moment,
    )

    stored = store.get(record.id)

    assert reopened
    assert stored.finalized_at is None
    assert stored.reopened_at is not None
    assert stored.current_booking_status == BookingStatus.BOOKED


def test_reopening_never_touches_first_evidence(store):
    record = executed(store)
    booked = reservation(booked_date="2026-09-06T16:30:00.000Z")

    reconciler(store, FakeReader([booked])).run_once(now=NOW)

    before = {
        k: v
        for k, v in vars(store.get(record.id)).items()
        if k.startswith("first_")
    }

    reconciler(store, FakeReader([])).reopen_if_changed(
        store.get(record.id),
        [],
        now=NOW + datetime.timedelta(days=40),
    )

    after = {
        k: v
        for k, v in vars(store.get(record.id)).items()
        if k.startswith("first_")
    }

    assert after == before


def test_the_finalization_window_is_named_configuration():
    """Not a magic number buried in a branch, and documented as a policy."""
    import app.pricing_outcomes as outcomes

    assert outcomes.FINALIZATION_DAYS_AFTER_CHECKOUT == 30
    assert outcomes.FINALIZATION_STABLE_PASSES == 2
    assert outcomes.FINALIZATION_MIN_PASSES == 3

    with open(outcomes.__file__) as handle:
        source = handle.read()

    assert "policy choice" in source
    assert "not a measured" in source.lower()


# -- structural safety ----------------------------------------------------


def test_the_reconciler_exposes_no_pricing_writer(store):
    """Absence is the safety property, not an unused attribute."""
    import inspect

    reader = FakeReader([])
    engine = reconciler(store, reader)

    assert not hasattr(engine, "_writer")
    assert not hasattr(engine, "writer")

    signature = inspect.signature(PricingOutcomeReconciler.__init__)

    assert "writer" not in signature.parameters

    body = "".join(
        line
        for line in inspect.getsource(PricingOutcomeReconciler).splitlines()
        if not line.lstrip().startswith("#")
    )

    for forbidden in ("set_override", "remove_override", "_writer"):
        assert forbidden not in body

    # Structural, not textual: the store holds no provider object at all, so
    # there is nothing on it through which a price could be changed.
    holder = PricingOutcomeStore(database=store._database)

    assert set(vars(holder)) == {"_database"}

    assert "reader" not in inspect.signature(
        PricingOutcomeStore.__init__
    ).parameters


def test_one_provider_fetch_per_listing_not_per_row(store):
    """Five nights on one listing must cost one request, not five."""
    for day in range(10, 15):
        executed(store, stay_date=f"2026-09-{day}")

    reader = FakeReader([])

    reconciler(store, reader).run_once(now=NOW)

    assert len(reader.calls) == 1

    _, _, start, end = reader.calls[0]

    assert start <= "2026-09-10"
    assert end >= "2026-09-14"


# -- projection -----------------------------------------------------------


def test_no_reservation_id_is_ever_projected(store):
    record = executed(store)
    booked = reservation(booked_date="2026-09-06T16:30:00.000Z")

    reconciler(store, FakeReader([booked])).run_once(now=NOW)

    payload = to_payload(store.get(record.id))

    assert "first_reservation_id" not in payload
    assert "current_reservation_id" not in payload
    assert "r-1" not in str(payload)
    # ...but the channel is projected, because it is a coarse category.
    assert payload["first_booking_channel"] == "bcom"


def test_the_projection_carries_the_decision_evidence(store):
    record = executed(store)

    payload = to_payload(store.get(record.id))

    assert payload["history_adr"] == pytest.approx(149.5)
    assert payload["history_sample_count"] == 9
    assert payload["market_signal_conflict"] is True
    assert payload["observed_commission_rate"] == pytest.approx(23.0)
    assert payload["price_before"] == pytest.approx(182.0)


# -- health ---------------------------------------------------------------


def test_health_distinguishes_a_stopped_job_from_a_quiet_market(store):
    """The failure this exists to catch.

    A stopped reconciler and a night nobody booked both report zero bookings.
    The unreconciled age is what separates them.
    """
    executed(store)

    health = store.health(now=NOW + datetime.timedelta(days=3))

    assert health["outcomes"] == 1
    assert health["unreconciled"] == 1
    assert health["oldest_unreconciled_age_hours"] > 24
    assert health["last_successful_reconciliation_at"] is None
    assert health["booked_currently"] == 0


def test_health_is_fresh_once_a_pass_has_run(store):
    executed(store)

    reconciler(store, FakeReader([])).run_once(now=NOW)

    health = store.health(now=NOW)

    assert health["unreconciled"] == 0
    assert health["oldest_unreconciled_age_hours"] is None
    assert health["last_successful_reconciliation_at"] is not None
    assert health["awaiting_first_booking"] == 1
