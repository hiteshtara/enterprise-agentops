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


def test_either_proven_expiry_route_would_unblock_a_price_write(monkeypatch):
    """Two independent routes exist; neither is taken yet."""
    import app.pricing_config as config

    monkeypatch.setattr(config, "CLEANUP_STRATEGY_VERIFIED", True)

    assert config.unverified_reason("RAISE") is None

    monkeypatch.setattr(config, "CLEANUP_STRATEGY_VERIFIED", False)
    monkeypatch.setattr(config, "EXPIRY_SEMANTICS_VERIFIED", True)

    assert config.unverified_reason("RAISE") is None
