"""Explicit cleanup of temporary fixed-price overrides.

Every value here is invented. Nothing in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.connectors.pricelabs.errors import PriceLabsUnavailable
from app.connectors.pricelabs.write_client import (
    WriteOutcome,
    WriteResult,
)
from app.pricing_cleanup import (
    CLAIM_LEASE_SECONDS,
    MARKER_PREFIX,
    MAX_REASON_LENGTH,
    CleanupState,
    PricingCleanupStore,
    build_reason,
    check_ownership,
    default_cleanup_at,
    marker_of,
)
from app.pricing_cleanup_runner import (
    OWNERSHIP_LOST,
    PricingCleanupRunner,
    summarise,
)

BUNKERS = "680444___747423"

STAY = "2026-09-20"

NOW = datetime.datetime(2026, 9, 5, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def store(database) -> PricingCleanupStore:
    return PricingCleanupStore(database=database)


def active_record(store, *, price=246.0, adopted=False):
    """A record already written and confirmed, as the runner will find it."""
    if adopted:
        return store.adopt(
            listing_id=BUNKERS,
            pms="lodgify",
            stay_date=STAY,
            new_price=price,
            currency="USD",
            cleanup_at=NOW.isoformat(),
            provider_created_at="2026-09-04T18:52:49.000Z",
            approval_id="ap-legacy",
            run_id="run-legacy",
            resolution="adopted; predates V2 and carries no marker",
        )

    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=200.0,
        new_price=price,
        currency="USD",
        cleanup_at=NOW.isoformat(),
        approval_id="ap-1",
        run_id="run-1",
    )

    store.mark_active(
        record.id,
        provider_created_at="2026-09-05T09:00:00.000Z",
        reason_sent=build_reason(record.marker, "because"),
    )

    return store.get(record.id)


def provider_override(record, **over):
    """What PriceLabs would return for a record's own override."""
    row = {
        "date": record.stay_date,
        "price": str(round(record.new_price)),
        "price_type": "fixed",
        "currency": "USD",
        "reason": (
            "" if record.adopted else build_reason(record.marker, "because")
        ),
        "created_at": record.provider_created_at,
        "updated_at": record.provider_created_at,
    }

    row.update(over)

    return row


class FakeReader:
    def __init__(self, override=None, fail=False):
        self.override = override
        self.fail = fail

    def overrides(self, listing_id, pms):
        if self.fail:
            raise PriceLabsUnavailable("down")

        return [self.override] if self.override else []


class RecordingWriter:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def remove_override(self, listing_id, pms, stay_date, **kw):
        self.calls.append((listing_id, stay_date))

        if self.raises:
            raise self.raises

        return self.result or WriteResult(
            outcome=WriteOutcome.CONFIRMED_APPLIED,
            message="removed",
            stay_date=stay_date,
            old_price=246.0,
        )


def run(store, reader, writer, monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    return PricingCleanupRunner(store, reader, writer).run_once(now=NOW)


# -- the marker ------------------------------------------------------------


def test_the_marker_leads_the_reason_so_truncation_cannot_remove_it():
    reason = build_reason("abc-123", "a" * 500)

    assert reason.startswith(f"{MARKER_PREFIX}abc-123: ")
    assert len(reason) <= MAX_REASON_LENGTH
    assert marker_of(reason) == "abc-123"


def test_a_human_written_reason_carries_no_marker():
    for text in ("", "note to self", "AGENTGUARD", "AGENTGUARD:", None):
        assert marker_of(text) is None


def test_cleanup_at_is_bounded_by_both_arrival_and_lifetime():
    soon = default_cleanup_at(datetime.date(2026, 9, 8), NOW)
    far = default_cleanup_at(datetime.date(2026, 12, 1), NOW)

    # Two clear days before a near arrival.
    assert soon.date() == datetime.date(2026, 9, 6)
    # Never more than a week out, however distant the stay.
    assert far.date() == datetime.date(2026, 9, 12)


# -- the row comes first ---------------------------------------------------


def test_a_record_exists_before_the_override_is_written(store):
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=None,
        new_price=246.0,
        currency="USD",
        cleanup_at=NOW.isoformat(),
    )

    assert record.state == CleanupState.PENDING_WRITE.value
    assert record.marker == record.id
    assert store.get(record.id) is not None


def test_cleanup_at_is_stored_not_recomputed(store):
    fixed = "2027-01-01T00:00:00+00:00"

    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=None,
        new_price=246.0,
        currency="USD",
        cleanup_at=fixed,
    )

    assert store.get(record.id).cleanup_at == fixed


# -- ownership -------------------------------------------------------------


def test_ownership_holds_when_marker_price_and_timestamps_match(store):
    record = active_record(store)

    assert check_ownership(record, provider_override(record)).owned


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("price", "999", "price is now"),
        ("reason", "someone else's note", "no AgentGuard marker"),
        ("reason", build_reason("other-uuid", "x"), "different record"),
        ("created_at", "2020-01-01T00:00:00.000Z", "different time"),
        ("updated_at", "2030-01-01T00:00:00.000Z", "modified since"),
    ],
)
def test_ownership_refuses_when_anything_differs(store, field, value, expected):
    record = active_record(store)

    check = check_ownership(record, provider_override(record, **{field: value}))

    assert check.refused
    assert expected in check.reason


def test_an_absent_override_is_not_owned(store):
    assert check_ownership(active_record(store), None).refused


# -- the runner ------------------------------------------------------------


def test_the_happy_path_sends_exactly_one_delete(store, monkeypatch):
    record = active_record(store)

    writer = RecordingWriter()

    outcomes = run(store, FakeReader(provider_override(record)), writer, monkeypatch)

    assert [o.state for o in outcomes] == [CleanupState.CLEANED_UP]
    assert writer.calls == [(BUNKERS, STAY)]
    assert store.get(record.id).state == CleanupState.CLEANED_UP.value


def test_a_failed_ownership_check_sends_nothing(store, monkeypatch):
    record = active_record(store)

    writer = RecordingWriter()

    stranger = provider_override(record, reason="a human put this here")

    outcomes = run(store, FakeReader(stranger), writer, monkeypatch)

    assert [o.state for o in outcomes] == [CleanupState.NEEDS_REVIEW]
    assert writer.calls == [], "a human's override must never be deleted"
    assert "no AgentGuard marker" in store.get(record.id).resolution


def test_an_already_absent_override_is_vanished_not_failed(store, monkeypatch):
    active_record(store)

    writer = RecordingWriter()

    outcomes = run(store, FakeReader(None), writer, monkeypatch)

    assert [o.state for o in outcomes] == [CleanupState.VANISHED]
    assert writer.calls == []


def test_an_unknown_removal_is_never_retried(store, monkeypatch):
    record = active_record(store)

    writer = RecordingWriter(raises=PriceLabsUnavailable("timeout"))

    outcomes = run(store, FakeReader(provider_override(record)), writer, monkeypatch)

    assert [o.state for o in outcomes] == [CleanupState.UNKNOWN_CLEANUP_STATE]
    assert len(writer.calls) == 1

    # A second pass must not pick it up again.
    again = run(store, FakeReader(provider_override(record)), writer, monkeypatch)

    assert again == []
    assert len(writer.calls) == 1


def test_a_removal_that_did_not_take_effect_needs_review(store, monkeypatch):
    record = active_record(store)

    writer = RecordingWriter(
        result=WriteResult(
            outcome=WriteOutcome.CONFIRMED_FAILED,
            message="provider refused",
            stay_date=STAY,
        )
    )

    outcomes = run(store, FakeReader(provider_override(record)), writer, monkeypatch)

    assert [o.state for o in outcomes] == [CleanupState.NEEDS_REVIEW]


def test_a_provider_read_failure_leaves_the_record_active(store, monkeypatch):
    """A failed *read* is retried next run; only a DELETE is never repeated."""
    record = active_record(store)

    writer = RecordingWriter()

    outcomes = run(store, FakeReader(fail=True), writer, monkeypatch)

    assert [o.state for o in outcomes] == [CleanupState.ACTIVE]
    assert writer.calls == []
    assert store.get(record.id).state == CleanupState.ACTIVE.value


def test_cleanup_respects_the_kill_switches(store, monkeypatch):
    record = active_record(store)

    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    from app.connectors.pricelabs.write_client import PriceLabsWriteClient

    reader = FakeReader(provider_override(record))

    runner = PricingCleanupRunner(
        store,
        reader,
        PriceLabsWriteClient(reader=reader, api_key_provider=lambda: "k"),
    )

    outcomes = runner.run_once(now=NOW)

    assert [o.state for o in outcomes] == [CleanupState.ACTIVE]
    assert store.get(record.id).state == CleanupState.ACTIVE.value


def test_a_record_is_only_due_once_its_cleanup_at_arrives(store, monkeypatch):
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=None,
        new_price=246.0,
        currency="USD",
        cleanup_at="2027-01-01T00:00:00+00:00",
    )

    store.mark_active(record.id, "2026-09-05T09:00:00.000Z", "r")

    assert store.due(now=NOW) == []


def test_overdue_records_surface_even_if_the_runner_never_ran(store):
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=None,
        new_price=246.0,
        currency="USD",
        cleanup_at="2026-09-01T00:00:00+00:00",
    )

    store.mark_active(record.id, "2026-08-30T09:00:00.000Z", "r")

    overdue = store.overdue(now=NOW)

    assert [r.id for r in overdue] == [record.id]


# -- the adopted pre-V2 override ------------------------------------------


def test_the_adopted_record_is_exempt_from_the_marker_check(store):
    record = active_record(store, adopted=True)

    assert record.adopted is True
    assert record.marker is None

    # It carries an empty reason, as every pre-V2 override does.
    assert check_ownership(record, provider_override(record)).owned


def test_the_adopted_record_still_requires_price_and_timestamps(store):
    record = active_record(store, adopted=True)

    assert check_ownership(record, provider_override(record, price="1")).refused
    assert check_ownership(
        record, provider_override(record, updated_at="2031-01-01T00:00:00.000Z")
    ).refused


def test_only_an_adopted_record_may_skip_the_marker(store):
    """The exemption is stored on the row, so it cannot spread."""
    normal = active_record(store)

    assert normal.adopted is False
    assert check_ownership(normal, provider_override(normal, reason="")).refused


# -- the gate --------------------------------------------------------------


def test_the_cleanup_strategy_is_verified_and_unblocks_only_raise():
    """The gate reflects the 2026-09-05 live Arboretum lifecycle, and no more.

    Flipping it released RAISE from *this* gate. It did not release LOWER,
    which carries an independent Booking.com channel-discount gate, and it did
    not enable any write -- both runtime switches are separate and off.
    """
    from app.pricing_config import (
        BANDS,
        CLEANUP_STRATEGY_VERIFIED,
        unverified_reason,
    )

    assert CLEANUP_STRATEGY_VERIFIED is True

    for band in BANDS:
        assert unverified_reason("RAISE", band.listing_id) is None

        blocked = unverified_reason("LOWER", band.listing_id)

        assert blocked is not None and "Booking.com" in blocked, band.slug


def test_explicit_cleanup_is_the_sole_unlock_for_a_price_write(monkeypatch):
    """Provider-side expiry is not an alternate permission path.

    It is unowned, unobservable in the moment, and leaves no per-override audit
    trail. Even proven it would show the mechanism worked once, not that it
    worked for a given override on a given day.
    """
    import app.pricing_config as config

    monkeypatch.setattr(config, "CLEANUP_STRATEGY_VERIFIED", False)
    monkeypatch.setattr(config, "EXPIRY_SEMANTICS_VERIFIED", True)

    for action in ("LOWER", "RAISE"):
        assert config.unverified_reason(action) is not None, (
            "proven lead_time_expiry must not unlock a fixed-price write"
        )

    monkeypatch.setattr(config, "CLEANUP_STRATEGY_VERIFIED", True)
    monkeypatch.setattr(config, "EXPIRY_SEMANTICS_VERIFIED", False)

    # RAISE clears on the cleanup gate alone. LOWER does not: it carries a
    # second, independent gate for channel discount exposure.
    assert config.unverified_reason("RAISE", BUNKERS) is None
    assert config.unverified_reason("LOWER", BUNKERS) is not None

    monkeypatch.setattr(config, "BOOKING_COM_DISCOUNT_EXPOSURE_VERIFIED", True)

    assert config.unverified_reason("LOWER", BUNKERS) is None


# -- provider_created_at is mandatory -------------------------------------


def test_a_row_without_a_provider_creation_time_never_becomes_active(store):
    """Three of four checks is a weaker standard nothing downstream announces."""
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=None,
        new_price=246.0,
        currency="USD",
        cleanup_at=NOW.isoformat(),
    )

    state = store.mark_active(record.id, None, "reason")

    assert state is CleanupState.NEEDS_REVIEW

    stored = store.get(record.id)

    assert stored.state == CleanupState.NEEDS_REVIEW.value
    assert "no creation time" in stored.resolution


def test_a_row_with_a_creation_time_becomes_active(store):
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=None,
        new_price=246.0,
        currency="USD",
        cleanup_at=NOW.isoformat(),
    )

    assert (
        store.mark_active(record.id, "2026-09-05T09:00:00.000Z", "r")
        is CleanupState.ACTIVE
    )


def test_ownership_refuses_a_v2_row_missing_its_creation_time(store):
    """Defence in depth: even if such a row existed, cleanup would not act."""
    record = active_record(store)

    record.provider_created_at = None

    check = check_ownership(record, provider_override(record))

    assert check.refused
    assert "no provider creation time" in check.reason


def test_a_row_refused_at_confirmation_is_never_due_for_cleanup(store):
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=None,
        new_price=246.0,
        currency="USD",
        cleanup_at=NOW.isoformat(),
    )

    store.mark_active(record.id, None, "reason")

    assert store.due(now=NOW) == []


# -- reaching ACTIVE: every confirming check, and only then ----------------


def pending(store, **over):
    fields = {
        "listing_id": BUNKERS,
        "pms": "lodgify",
        "stay_date": STAY,
        "old_price": 200.0,
        "new_price": 246.0,
        "currency": "USD",
        "cleanup_at": NOW.isoformat(),
        "approval_id": "ap-1",
        "run_id": "run-1",
    }

    fields.update(over)

    return store.record_intent(**fields)


def test_a_row_whose_override_was_modified_not_created_needs_review(store):
    """`updated_at != created_at` means the POST landed on an existing row.

    That override is somebody else's, or an earlier one of ours. Either way its
    provenance is not what this record claims, so it is not ours to remove on
    this record's authority -- and cleanup must never be attempted for it.
    """
    record = pending(store)

    state = store.mark_active(
        record.id,
        "2026-09-05T09:00:00.000Z",
        "reason",
        provider_updated_at="2026-09-05T09:04:11.000Z",
    )

    assert state is CleanupState.NEEDS_REVIEW
    assert store.get(record.id).state == CleanupState.NEEDS_REVIEW.value
    assert store.due(now=NOW) == [], "a refused row is never due for cleanup"


def test_a_row_becomes_active_only_when_every_confirming_check_passes(store):
    """The positive case, stated alongside the refusals it sits between."""
    record = pending(store)

    stamp = "2026-09-05T09:00:00.000Z"

    state = store.mark_active(record.id, stamp, "reason", provider_updated_at=stamp)

    assert state is CleanupState.ACTIVE

    stored = store.get(record.id)

    assert stored.provider_created_at == stamp
    assert [row.id for row in store.due(now=NOW)] == [record.id]


def test_the_confirming_re_read_carries_both_timestamps():
    """The write client must supply what `mark_active` is asked to check.

    Guarded because the check is only as good as the field feeding it: a
    `WriteResult` that stopped reporting `updated_at` would silently turn the
    modified-not-created refusal into a no-op.
    """
    from app.connectors.pricelabs.client import PriceLabsClient
    from app.connectors.pricelabs.write_client import PriceLabsWriteClient

    class Reader(PriceLabsClient):
        def __init__(self):
            pass

        def overrides(self, listing_id, pms):
            return [
                {
                    "date": STAY,
                    "price": "246",
                    "reason": "AGENTGUARD:m-1: because",
                    "created_at": "2026-09-05T09:00:00.000Z",
                    "updated_at": "2026-09-05T09:00:00.000Z",
                }
            ]

    client = PriceLabsWriteClient(reader=Reader(), api_key_provider=lambda: "k")

    result = client._verify(
        BUNKERS,
        "lodgify",
        STAY,
        expect_present=True,
        expected_price=246.0,
        old_price=None,
        acknowledged=True,
        expected_reason="AGENTGUARD:m-1: because",
    )

    assert result.outcome is WriteOutcome.CONFIRMED_APPLIED
    assert result.provider_created_at == "2026-09-05T09:00:00.000Z"
    assert result.provider_updated_at == "2026-09-05T09:00:00.000Z"


# -- what an hourly pass does, and does not, do ---------------------------


def test_a_second_pass_after_an_unknown_delete_sends_nothing(store, monkeypatch):
    """UNKNOWN is terminal. The next hour must not try again.

    This is the property that makes an ambiguous removal safe: the override may
    already be gone, and a second DELETE against a date a person has since
    re-pinned would destroy their work.
    """
    record = active_record(store)

    reader = FakeReader(provider_override(record))

    writer = RecordingWriter(raises=PriceLabsUnavailable("timeout"))

    run(store, reader, writer, monkeypatch)

    assert len(writer.calls) == 1
    assert store.get(record.id).state == CleanupState.UNKNOWN_CLEANUP_STATE.value

    # The next hourly pass, against a store that still holds the row.
    again = RecordingWriter()

    run(store, reader, again, monkeypatch)

    assert again.calls == [], "an unknown removal is never retried"


def test_a_pass_leaves_a_not_yet_due_row_untouched(store, monkeypatch):
    """Only rows whose cleanup_at has arrived are processed."""
    record = active_record(store)

    reader = FakeReader(provider_override(record))

    writer = RecordingWriter()

    earlier = NOW - datetime.timedelta(hours=1)

    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    outcomes = PricingCleanupRunner(store, reader, writer).run_once(now=earlier)

    assert outcomes == []
    assert writer.calls == []
    assert store.get(record.id).state == CleanupState.ACTIVE.value


def test_a_read_failure_leaves_the_row_for_the_next_hourly_pass(store, monkeypatch):
    """A transient read failure is retried; only a DELETE never is."""
    record = active_record(store)

    run(store, FakeReader(fail=True), RecordingWriter(), monkeypatch)

    assert store.get(record.id).state == CleanupState.ACTIVE.value

    writer = RecordingWriter()

    run(store, FakeReader(provider_override(record)), writer, monkeypatch)

    assert len(writer.calls) == 1
    assert store.get(record.id).state == CleanupState.CLEANED_UP.value


def test_a_booked_night_is_still_cleaned_up(store, monkeypatch):
    """Cleanup is owed whatever happened to the night since.

    Removing an override on a booked night touches the pricing override and
    nothing else -- not the reservation, not the guest's agreed rate. The
    obligation to remove what AgentGuard wrote does not lapse because the date
    sold, and leaving it would strand exactly the pin this design prevents.
    """
    record = active_record(store)

    override = provider_override(record)

    reader = FakeReader(override)

    writer = RecordingWriter()

    run(store, reader, writer, monkeypatch)

    assert len(writer.calls) == 1
    assert store.get(record.id).state == CleanupState.CLEANED_UP.value


def test_cleanup_never_sets_a_price(store, monkeypatch):
    """The runner has no path to a price-setting call.

    Structural, not behavioural: a writer that records `set_override` proves
    the runner never reaches for it, and the absence is the safety property.
    """
    record = active_record(store)

    class Watching(RecordingWriter):
        def set_override(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("cleanup must never set a price")

    writer = Watching()

    run(store, FakeReader(provider_override(record)), writer, monkeypatch)

    assert [call[0] for call in writer.calls] == [BUNKERS]


def test_a_cleanup_audit_names_the_approval_that_created_the_obligation(
    store,
    monkeypatch,
):
    """Nobody approves a cleanup, so the trail has to reach back to who did."""
    record = active_record(store)

    class Recorder:
        def __init__(self):
            self.events = []

        def record(self, event, details, run_id=None):
            self.events.append((event, details, run_id))

    audit = Recorder()

    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)

    PricingCleanupRunner(
        store,
        FakeReader(provider_override(record)),
        RecordingWriter(),
        audit=audit,
    ).run_once(now=NOW)

    assert len(audit.events) == 1

    event, details, run_id = audit.events[0]

    assert event == "PRICING_CLEANUP"
    assert details["approval_id"] == "ap-1"
    assert details["cleanup_id"] == record.id
    assert details["state"] == CleanupState.CLEANED_UP.value
    assert run_id == "run-1"


def test_a_pass_is_inert_while_the_kill_switches_are_off(store, monkeypatch):
    """Both switches still gate cleanup, and the row survives to be retried."""
    record = active_record(store)

    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)
    monkeypatch.delenv("PRICELABS_AUTOMATION_ENABLED", raising=False)

    from app.connectors.pricelabs.write_client import PricingWritesDisabled

    class Gated(RecordingWriter):
        def remove_override(self, listing_id, pms, stay_date, **kw):
            self.calls.append((listing_id, stay_date))

            raise PricingWritesDisabled("ENABLE_PRICING_WRITES is not enabled")

    writer = Gated()

    PricingCleanupRunner(store, FakeReader(provider_override(record)), writer).run_once(
        now=NOW
    )

    assert store.get(record.id).state == CleanupState.ACTIVE.value


# -- the durable claim: two processes, one DELETE -------------------------
#
# The operator route and the hourly job drive the same runner and know nothing
# about each other. Selecting a due row is not permission to act on it; the
# claim is. These tests build genuinely separate store and runner objects over
# one database, because a shared instance would prove nothing about two
# processes -- it is the database that has to arbitrate.


def two_runners(database, record, writer_a, writer_b):
    """Two independent stacks over one database, as two processes would be."""
    store_a = PricingCleanupStore(database=database)
    store_b = PricingCleanupStore(database=database)

    assert store_a is not store_b

    return (
        PricingCleanupRunner(store_a, FakeReader(provider_override(record)), writer_a),
        PricingCleanupRunner(store_b, FakeReader(provider_override(record)), writer_b),
        store_a,
    )


def switches_on(monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", BUNKERS)


def test_two_concurrent_runners_send_exactly_one_delete(store, database, monkeypatch):
    """The defect this exists to prevent: both processes deleting.

    Without a claim both runners see the same ACTIVE due row, both prove
    ownership against the same override, and both send a DELETE. The second is
    the dangerous one -- if a person re-pinned the date in between, it destroys
    their pin.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    a_writer, b_writer = RecordingWriter(), RecordingWriter()

    runner_a, runner_b, observer = two_runners(database, record, a_writer, b_writer)

    out_a = runner_a.run_once(now=NOW)
    out_b = runner_b.run_once(now=NOW)

    total = len(a_writer.calls) + len(b_writer.calls)

    assert total == 1, "one obligation may produce at most one DELETE attempt"
    assert len(out_a) == 1 and out_b == [], "only the claimant processes the row"
    assert b_writer.calls == [], "the loser must not touch the provider at all"
    assert observer.get(record.id).state == CleanupState.CLEANED_UP.value


def test_the_loser_of_a_claim_does_not_even_read_the_provider(
    store,
    database,
    monkeypatch,
):
    """The claim precedes the read, so a loser holds no ownership proof.

    Its reader raises on any call, so a single provider read would fail the
    test rather than pass silently.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    other = PricingCleanupStore(database=database)

    assert other.claim(record.id, "held-by-someone-else", now=NOW)

    class ExplodingReader:
        def overrides(self, listing_id, pms):  # pragma: no cover - must not run
            raise AssertionError("a runner that lost the claim must not read")

    writer = RecordingWriter()

    outcomes = PricingCleanupRunner(
        PricingCleanupStore(database=database),
        ExplodingReader(),
        writer,
    ).run_once(now=NOW)

    assert outcomes == []
    assert writer.calls == []


def test_a_claim_is_won_by_exactly_one_of_many_attempts(store):
    """The compare-and-swap itself, isolated from the runner."""
    record = active_record(store)

    won = [store.claim(record.id, f"token-{i}", now=NOW) for i in range(5)]

    assert won.count(True) == 1
    assert won[0] is True, "the first attempt takes it; the rest are refused"
    assert store.get(record.id).state == CleanupState.CLAIMED.value


def test_a_claimed_row_is_not_offered_again_while_its_lease_holds(store):
    record = active_record(store)

    assert store.claim(record.id, "mine", now=NOW)

    # One minute later, well inside the fifteen-minute lease.
    soon = NOW + datetime.timedelta(minutes=1)

    assert store.due(now=soon) == []


# -- crash recovery -------------------------------------------------------


def test_a_lease_that_lapsed_before_the_delete_is_never_taken_over(
    store,
    database,
    monkeypatch,
):
    """Case A: the process died after claiming and before deleting.

    We cannot tell that from a process frozen one line *before* its DELETE, or
    one that already sent it. So the row is not taken over and retried: it is
    reconciled to NEEDS_REVIEW with nothing sent, and a person decides.

    This is deliberately conservative. A stranded AgentGuard override costs
    someone a few minutes; deleting a pricing decision a person made in the
    meantime cannot be undone.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    assert store.claim(record.id, "died-holding-this", now=NOW)

    # ...and nothing more happens. The process is gone.
    later = NOW + datetime.timedelta(seconds=CLAIM_LEASE_SECONDS + 1)

    assert store.due(now=later) == [], "a lapsed claim is never offered as work"
    assert [r.id for r in store.expired_claims(now=later)] == [record.id]

    writer = RecordingWriter()

    PricingCleanupRunner(
        PricingCleanupStore(database=database),
        FakeReader(provider_override(record)),
        writer,
    ).run_once(now=later)

    assert writer.calls == [], "elapsed time is never permission to delete"

    settled = store.get(record.id)

    assert settled.state == CleanupState.NEEDS_REVIEW.value
    assert "expired without being released" in settled.resolution
    assert "still in place" in settled.resolution


def test_recovery_after_a_delete_that_was_never_recorded_sends_nothing(
    store,
    database,
    monkeypatch,
):
    """Case B: the DELETE landed, then the process died before writing it down.

    The provider may be read to describe what a person will find -- here, that
    the override is already gone -- but a second DELETE is never sent.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    assert store.claim(record.id, "died-after-deleting", now=NOW)

    later = NOW + datetime.timedelta(seconds=CLAIM_LEASE_SECONDS + 1)

    writer = RecordingWriter()

    PricingCleanupRunner(
        PricingCleanupStore(database=database),
        FakeReader(None),  # the provider no longer has it
        writer,
    ).run_once(now=later)

    assert writer.calls == [], "recovery must never re-issue a DELETE"

    settled = store.get(record.id)

    assert settled.state == CleanupState.NEEDS_REVIEW.value
    assert "no longer at PriceLabs" in settled.resolution


def test_reconciliation_reports_rather_than_fails_when_the_provider_is_down(
    store,
    database,
    monkeypatch,
):
    """The diagnostic read is a courtesy, not a dependency.

    A provider that cannot be reached must not leave the row stuck under a
    dead claim -- the point of reconciling is to get it in front of a person.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    assert store.claim(record.id, "died-holding-this", now=NOW)

    later = NOW + datetime.timedelta(seconds=CLAIM_LEASE_SECONDS + 1)

    writer = RecordingWriter()

    PricingCleanupRunner(
        PricingCleanupStore(database=database),
        FakeReader(fail=True),
        writer,
    ).run_once(now=later)

    settled = store.get(record.id)

    assert writer.calls == []
    assert settled.state == CleanupState.NEEDS_REVIEW.value
    assert "could not be read" in settled.resolution


def test_recovery_never_deletes_an_override_a_person_has_since_created(
    store,
    database,
    monkeypatch,
):
    """The reason a stale claim may not shortcut to DELETE.

    Between the crash and the recovery a human pinned the date themselves.
    Ownership fails, nothing is sent, and it goes to a person.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    assert store.claim(record.id, "died-holding-this", now=NOW)

    theirs = provider_override(record, reason="Owner: holiday weekend", price="399")

    later = NOW + datetime.timedelta(seconds=CLAIM_LEASE_SECONDS + 1)

    writer = RecordingWriter()

    PricingCleanupRunner(
        PricingCleanupStore(database=database),
        FakeReader(theirs),
        writer,
    ).run_once(now=later)

    assert writer.calls == [], "their pin must survive"
    assert store.get(record.id).state == CleanupState.NEEDS_REVIEW.value


def test_a_lapsed_owner_cannot_overwrite_a_settled_verdict(store, database):
    """The lease-expiry hazard, guarded by the token.

    A slow process wakes to resolve a row whose claim reconciliation has since
    settled. Its stale verdict must not land on top.
    """
    record = active_record(store)

    assert store.claim(record.id, "slow-process", now=NOW)

    later = NOW + datetime.timedelta(seconds=CLAIM_LEASE_SECONDS + 1)

    other = PricingCleanupStore(database=database)

    assert other.resolve(
        record.id,
        CleanupState.NEEDS_REVIEW,
        "reconciled: the claim expired",
        expected_token="slow-process",
    )

    # The reconciler cleared the token, so the original owner now holds nothing.
    stale = store.resolve(
        record.id,
        CleanupState.CLEANED_UP,
        "stale verdict from a lapsed claim",
        expected_token="slow-process",
    )

    assert stale is False
    assert store.get(record.id).state == CleanupState.NEEDS_REVIEW.value
    assert store.get(record.id).resolution == "reconciled: the claim expired"

    # And it is not silently re-offered as work, in either queue.
    assert store.due(now=later) == []
    assert store.expired_claims(now=later) == []


def test_a_pass_that_could_not_read_returns_the_row_immediately(
    store,
    database,
    monkeypatch,
):
    """A released claim does not make the next pass wait out the lease.

    Nothing was decided and nothing was sent, so the obligation is unchanged
    and should be retryable at once.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    PricingCleanupRunner(
        PricingCleanupStore(database=database),
        FakeReader(fail=True),
        RecordingWriter(),
    ).run_once(now=NOW)

    reloaded = store.get(record.id)

    assert reloaded.state == CleanupState.ACTIVE.value
    assert reloaded.claim_token is None
    assert [r.id for r in store.due(now=NOW)] == [record.id]


def test_a_settled_row_holds_no_claim(store, monkeypatch):
    """A terminal row must not look like work in progress."""
    switches_on(monkeypatch)

    record = active_record(store)

    run(store, FakeReader(provider_override(record)), RecordingWriter(), monkeypatch)

    settled = store.get(record.id)

    assert settled.state == CleanupState.CLEANED_UP.value
    assert settled.claim_token is None
    assert settled.lease_until is None


# -- the audit must not claim a transition that did not commit ------------
#
# `resolve` is conditional on still holding the claim, so a process whose claim
# was reconciled away writes nothing. What it must also not do is *say* it did:
# an audit event announcing CLEANED_UP for a row the database never moved makes
# the trail disagree with the thing it exists to describe.


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, event, details, run_id=None):
        self.events.append((event, details, run_id))

    def types(self):
        return [event for event, _, _ in self.events]

    def one(self, event):
        matching = [d for e, d, _ in self.events if e == event]

        assert len(matching) == 1, f"expected one {event}, got {len(matching)}"

        return matching[0]


def stale_owner_runner(store, database, record, reader, writer, audit):
    """A runner holding a claim that is about to be taken away from it."""
    assert store.claim(record.id, "slow-process", now=NOW)

    # Reconciliation settles it while the slow process is still working.
    PricingCleanupStore(database=database).resolve(
        record.id,
        CleanupState.NEEDS_REVIEW,
        "reconciled: the claim expired",
        expected_token="slow-process",
    )

    return PricingCleanupRunner(store, reader, writer, audit=audit)


def test_a_stale_owner_is_stopped_at_the_boundary_before_any_delete(
    store,
    database,
    monkeypatch,
):
    """The exact race. Proof of ownership is not permission to call out.

    The owner claims, reads, proves ownership, then stalls past its lease.
    Reconciliation settles the row to NEEDS_REVIEW and a person may already be
    acting on it. When the owner resumes it must send nothing -- and the
    DELETE_STARTED compare-and-swap is what stops it, because the token alone
    would only have stopped the database verdict, after the provider call.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    audit = Recorder()

    writer = RecordingWriter()

    runner = stale_owner_runner(
        store,
        database,
        record,
        FakeReader(provider_override(record)),
        writer,
        audit,
    )

    outcome = runner._process(store.get(record.id), "slow-process")

    assert writer.calls == [], "a lost claim must reach no provider write"
    assert "PRICING_CLEANUP" not in audit.types()

    stale = audit.one("PRICING_CLEANUP_STALE_OWNER")

    assert stale["attempted_state"] == CleanupState.DELETE_STARTED.value
    assert stale["provider_delete_attempted"] is False
    assert stale["cleanup_id"] == record.id
    assert stale["listing_id"] == BUNKERS
    assert stale["stay_date"] == STAY
    assert stale["approval_id"] == "ap-1"
    assert stale["claim_token"] == "slow-process"
    assert "lost before the provider was called" in stale["ownership"]

    assert outcome.committed is False
    assert outcome.reported_state == OWNERSHIP_LOST

    # The reconciler's verdict stands, untouched.
    settled = store.get(record.id)

    assert settled.state == CleanupState.NEEDS_REVIEW.value
    assert settled.resolution == "reconciled: the claim expired"


def test_a_delete_attempt_stays_auditable_even_if_the_verdict_is_refused(
    store,
    database,
):
    """Defence in depth for a refused verdict *after* a provider call.

    The boundary makes this unreachable through `_process` -- nothing clears
    the token once a row is in DELETE_STARTED -- so `_resolve` is exercised
    directly. If it ever did happen, the DELETE attempt is the one fact that
    must survive: it is what someone reconstructing the night needs.
    """
    record = active_record(store)

    audit = Recorder()

    runner = PricingCleanupRunner(
        store,
        FakeReader(provider_override(record)),
        RecordingWriter(),
        audit=audit,
    )

    outcome = runner._resolve(
        record,
        "a-token-nobody-holds",
        CleanupState.CLEANED_UP,
        "removed",
        deleted=True,
    )

    assert "PRICING_CLEANUP" not in audit.types()

    stale = audit.one("PRICING_CLEANUP_STALE_OWNER")

    assert stale["attempted_state"] == CleanupState.CLEANED_UP.value
    assert stale["provider_delete_attempted"] is True
    assert outcome.committed is False
    assert outcome.deleted is True
    assert outcome.reported_state == OWNERSHIP_LOST


def test_an_uncommitted_verdict_is_not_counted_as_a_settled_record(store):
    """The summary the route returns must not report a state that did not land."""
    record = active_record(store)

    runner = PricingCleanupRunner(
        store,
        FakeReader(provider_override(record)),
        RecordingWriter(),
        audit=Recorder(),
    )

    summary = summarise(
        [
            runner._resolve(
                record,
                "a-token-nobody-holds",
                CleanupState.CLEANED_UP,
                "removed",
                deleted=True,
            )
        ]
    )

    assert summary["by_state"] == {OWNERSHIP_LOST: 1}
    assert CleanupState.CLEANED_UP.value not in summary["by_state"]
    assert summary["deleted"] == 1, "the DELETE attempt is still counted"

    row = summary["records"][0]

    assert row["state"] == OWNERSHIP_LOST
    assert row["attempted_state"] == CleanupState.CLEANED_UP.value
    assert row["committed"] is False


def test_a_committed_resolution_audits_exactly_as_before(store, monkeypatch):
    """The ordinary path is unchanged -- one PRICING_CLEANUP, no stale event."""
    switches_on(monkeypatch)

    record = active_record(store)

    audit = Recorder()

    outcomes = PricingCleanupRunner(
        store,
        FakeReader(provider_override(record)),
        RecordingWriter(),
        audit=audit,
    ).run_once(now=NOW)

    assert audit.types() == ["PRICING_CLEANUP"]

    event = audit.one("PRICING_CLEANUP")

    assert event["state"] == CleanupState.CLEANED_UP.value
    assert event["deleted"] is True
    assert outcomes[0].committed is True
    assert outcomes[0].reported_state == CleanupState.CLEANED_UP.value


# -- CLAIMED must imply a claim token -------------------------------------


def test_a_claimed_row_with_no_token_is_failed_closed_not_bypassed(
    store,
    database,
    monkeypatch,
):
    """`claim` always sets a token, so this row should be impossible.

    If one ever exists it must still not become a way around token ownership.
    It is settled through its own compare-and-swap, sends nothing to the
    provider, and says an invariant broke.
    """
    switches_on(monkeypatch)

    record = active_record(store)

    # Fabricate the malformed row: CLAIMED, expired, no token.
    store._update(
        record.id,
        state=CleanupState.CLAIMED.value,
        claim_token=None,
        lease_until=(NOW - datetime.timedelta(hours=1)).isoformat(),
    )

    audit = Recorder()

    writer = RecordingWriter()

    outcomes = PricingCleanupRunner(
        PricingCleanupStore(database=database),
        FakeReader(provider_override(record)),
        writer,
        audit=audit,
    ).run_once(now=NOW)

    assert writer.calls == [], "a malformed row may never reach the provider"

    settled = store.get(record.id)

    assert settled.state == CleanupState.NEEDS_REVIEW.value
    assert "should be impossible" in settled.resolution

    event = audit.one("PRICING_CLEANUP")

    assert event["invariant_violation"] == "CLAIMED row carried no claim token"
    assert event["provider_delete_attempted"] is False
    assert outcomes[0].committed is True


def test_the_unclaimed_escape_hatch_cannot_settle_a_properly_claimed_row(store):
    """It is a compare-and-swap, not an unconditional write.

    A row someone legitimately holds must not be settleable through the path
    that exists for malformed ones.
    """
    record = active_record(store)

    assert store.claim(record.id, "rightful-owner", now=NOW)

    assert (
        store.resolve_unclaimed(
            record.id,
            CleanupState.NEEDS_REVIEW,
            "should not apply",
        )
        is False
    )

    assert store.get(record.id).state == CleanupState.CLAIMED.value
    assert store.get(record.id).claim_token == "rightful-owner"


# -- the delete boundary --------------------------------------------------
#
# A claim authorises *work*; DELETE_STARTED authorises the *call*. The gap
# between them is where a stalled process used to be able to delete an override
# somebody else had already taken over. These cover both sides of that fence.


def test_the_boundary_holder_deletes_and_the_reconciler_leaves_it_alone(
    store,
    database,
    monkeypatch,
):
    """B: the owner wins the boundary, so it -- and only it -- may call out."""
    switches_on(monkeypatch)

    record = active_record(store)

    assert store.claim(record.id, "owner", now=NOW)
    assert store.begin_delete(record.id, "owner") is True
    assert store.get(record.id).state == CleanupState.DELETE_STARTED.value

    # A reconciler runs while the owner is mid-call. It must find nothing.
    later = NOW + datetime.timedelta(seconds=CLAIM_LEASE_SECONDS + 1)

    other = PricingCleanupStore(database=database)

    assert other.due(now=later) == []
    assert other.expired_claims(now=later) == []

    reconciler_writer = RecordingWriter()

    PricingCleanupRunner(
        other,
        FakeReader(provider_override(record)),
        reconciler_writer,
    ).run_once(now=later)

    assert reconciler_writer.calls == [], "DELETE_STARTED is never automation's"

    # The owner completes, fenced by its own token.
    assert store.resolve(
        record.id,
        CleanupState.CLEANED_UP,
        "removed",
        expected_token="owner",
    )

    assert store.get(record.id).state == CleanupState.CLEANED_UP.value


def test_the_boundary_refuses_a_row_reconciliation_already_settled(store, database):
    """E: a verdict that landed first wins, and the stale owner cannot call."""
    record = active_record(store)

    assert store.claim(record.id, "slow-process", now=NOW)

    PricingCleanupStore(database=database).resolve(
        record.id,
        CleanupState.NEEDS_REVIEW,
        "reconciled",
        expected_token="slow-process",
    )

    assert store.begin_delete(record.id, "slow-process") is False
    assert store.get(record.id).state == CleanupState.NEEDS_REVIEW.value

    # And it cannot overwrite the verdict afterwards either.
    assert (
        store.resolve(
            record.id,
            CleanupState.CLEANED_UP,
            "stale",
            expected_token="slow-process",
        )
        is False
    )


def test_the_boundary_refuses_a_token_that_is_not_the_holders(store):
    record = active_record(store)

    assert store.claim(record.id, "owner", now=NOW)
    assert store.begin_delete(record.id, "someone-else") is False
    assert store.get(record.id).state == CleanupState.CLAIMED.value


def test_a_row_abandoned_at_the_boundary_is_never_retried(
    store,
    database,
    monkeypatch,
):
    """C and D: crashed before the call, or after it. Same answer either way.

    DELETE_STARTED cannot say which, and nothing in either system can settle
    it, so automation never touches the row again -- whatever the provider now
    shows.
    """
    switches_on(monkeypatch)

    # Two worlds: the override is still there (crashed before the call) and
    # the override is gone (crashed after it). Neither may be acted on.
    for still_present in (True, False):
        record = active_record(store)

        override = provider_override(record) if still_present else None

        assert store.claim(record.id, "died-here", now=NOW)
        assert store.begin_delete(record.id, "died-here")

        later = NOW + datetime.timedelta(days=3)

        writer = RecordingWriter()

        outcomes = PricingCleanupRunner(
            PricingCleanupStore(database=database),
            FakeReader(override),
            writer,
        ).run_once(now=later)

        assert writer.calls == [], "an ambiguous boundary is never retried"
        assert record.id not in [o.record_id for o in outcomes]
        assert store.get(record.id).state == CleanupState.DELETE_STARTED.value


def test_a_row_abandoned_at_the_boundary_is_surfaced_for_a_person(store):
    """It will never resolve itself, so it must be visible in the queue."""
    record = active_record(store)

    assert store.claim(record.id, "died-here", now=NOW)
    assert store.begin_delete(record.id, "died-here")

    later = NOW + datetime.timedelta(days=3)

    assert record.id in [r.id for r in store.open_records()]
    assert record.id in [r.id for r in store.overdue(now=later)]


def test_the_happy_path_crosses_the_boundary_exactly_once(store, monkeypatch):
    """F: ACTIVE -> CLAIMED -> DELETE_STARTED -> one DELETE -> CLEANED_UP."""
    switches_on(monkeypatch)

    record = active_record(store)

    seen: list[str] = []

    class Watching(RecordingWriter):
        def remove_override(self, listing_id, pms, stay_date, **kw):
            # The state at the moment the provider is called.
            seen.append(store.get(record.id).state)

            return super().remove_override(listing_id, pms, stay_date, **kw)

    writer = Watching()

    audit = Recorder()

    outcomes = PricingCleanupRunner(
        store,
        FakeReader(provider_override(record)),
        writer,
        audit=audit,
    ).run_once(now=NOW)

    assert seen == [CleanupState.DELETE_STARTED.value], (
        "the boundary must be committed before the provider is called"
    )
    assert len(writer.calls) == 1
    assert store.get(record.id).state == CleanupState.CLEANED_UP.value
    assert outcomes[0].committed is True
    assert audit.types() == ["PRICING_CLEANUP"]


def test_the_kill_switch_is_checked_before_the_boundary_is_taken(
    store,
    database,
    monkeypatch,
):
    """A parked boundary needs a person, so don't take one we cannot use."""
    monkeypatch.delenv("ENABLE_PRICING_WRITES", raising=False)
    monkeypatch.delenv("PRICELABS_AUTOMATION_ENABLED", raising=False)

    record = active_record(store)

    writer = RecordingWriter()

    PricingCleanupRunner(
        PricingCleanupStore(database=database),
        FakeReader(provider_override(record)),
        writer,
    ).run_once(now=NOW)

    reloaded = store.get(record.id)

    assert writer.calls == []
    assert reloaded.state == CleanupState.ACTIVE.value, (
        "the row goes back to the queue rather than parking at the boundary"
    )
    assert reloaded.claim_token is None
