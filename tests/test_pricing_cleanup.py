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
    MARKER_PREFIX,
    MAX_REASON_LENGTH,
    CleanupState,
    PricingCleanupStore,
    build_reason,
    check_ownership,
    default_cleanup_at,
    marker_of,
)
from app.pricing_cleanup_runner import PricingCleanupRunner

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


def test_the_cleanup_strategy_ships_unverified():
    from app.pricing_config import CLEANUP_STRATEGY_VERIFIED, unverified_reason

    assert CLEANUP_STRATEGY_VERIFIED is False

    for action in ("LOWER", "RAISE"):
        assert unverified_reason(action) is not None


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

    for action in ("LOWER", "RAISE"):
        assert config.unverified_reason(action) is None


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
