"""Fix A: what AgentGuard *intended* to send, recorded before it sends it.

The 2026-09-07 incident produced a row whose override was live at the
provider, whose marker and price both matched, and which carried
`reason_sent = NULL` -- because `reason_sent` was written only by
`mark_active`, which runs after a successful confirming re-read. That write's
re-read failed, so a person investigating had nothing local to compare the
provider's stored reason against.

These tests hold two things at once, and the second matters more:

  * the evidence is now recorded before the irreversible POST; and
  * **recording it grants nothing.** An ambiguous row still has no
    `provider_created_at`, is still `NEEDS_REVIEW`, and is still untouchable
    by automatic cleanup. A matching reason is not permission.

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.pricing_cleanup import (
    CleanupState,
    PricingCleanupStore,
    build_reason,
    check_ownership,
)

BUNKERS = "680444___747423"

STAY = "2026-09-20"

NOW = datetime.datetime(2026, 9, 7, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def store(database) -> PricingCleanupStore:
    return PricingCleanupStore(database=database)


def intent(store, price=196.0):
    """A PENDING_WRITE row, exactly as the write path creates it."""
    return store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=STAY,
        old_price=217.0,
        new_price=price,
        currency="USD",
        cleanup_at=NOW.isoformat(),
        approval_id="ap-1",
        run_id="run-1",
    )


# -- the evidence is recorded before the provider call --------------------


def test_reason_sent_exists_before_the_provider_post(store):
    record = intent(store)
    marked = build_reason(record.marker, "1d out and still open")

    assert store.get(record.id).reason_sent is None, "nothing sent yet"

    assert store.record_reason_sent(record.id, marked)

    stored = store.get(record.id)

    assert stored.reason_sent == marked
    # Recording intent changes nothing else. It is not a write.
    assert stored.state == CleanupState.PENDING_WRITE.value
    assert stored.provider_created_at is None


def test_the_write_path_records_intent_before_it_calls_the_provider():
    """Ordering asserted against the real source, not a re-enactment.

    The property is positional: `record_reason_sent` must appear between
    `build_reason` (which needs the marker) and `set_override` (the
    irreversible call). A staged fake could be arranged to pass while the
    production order was wrong; this cannot.
    """
    import inspect

    from app.connectors.pricelabs.pricing_tools import PriceLabsPricingTools

    source = inspect.getsource(PriceLabsPricingTools.apply_pricing_action)

    built = source.index("marked = build_reason(")
    recorded = source.index("record_reason_sent(")
    posted = source.index("self._writer.set_override(")

    assert built < recorded < posted, (
        "intent must be recorded after the marker exists and before the POST"
    )


# -- an ambiguous write keeps the evidence and gains nothing --------------


def test_an_unknown_write_retains_reason_sent(store):
    """The gap the incident exposed, closed."""
    record = intent(store)
    marked = build_reason(record.marker, "1d out and still open")

    store.record_reason_sent(record.id, marked)

    # The confirming re-read fails: no creation time comes back.
    state = store.mark_active(record.id, None, marked)

    stored = store.get(record.id)

    assert state is CleanupState.NEEDS_REVIEW
    assert stored.reason_sent == marked, "the intent survives the failure"


def test_provider_created_at_stays_none_on_an_unknown_write(store):
    record = intent(store)
    marked = build_reason(record.marker, "because")

    store.record_reason_sent(record.id, marked)
    store.mark_active(record.id, None, marked)

    assert store.get(record.id).provider_created_at is None


def test_an_unknown_write_stays_needs_review(store):
    record = intent(store)
    marked = build_reason(record.marker, "because")

    store.record_reason_sent(record.id, marked)
    store.mark_active(record.id, None, marked)

    assert store.get(record.id).state == CleanupState.NEEDS_REVIEW.value


def test_a_matching_reason_alone_never_permits_automatic_cleanup(store):
    """The load-bearing test of this change.

    Knowing what we sent must not become a shortcut to deleting. The row here
    has a perfect marker, a matching price and a byte-identical reason -- and
    is still not cleanable, because `provider_created_at` is absent and the
    row is not ACTIVE.
    """
    record = intent(store)
    marked = build_reason(record.marker, "because")

    store.record_reason_sent(record.id, marked)
    store.mark_active(record.id, None, marked)

    stored = store.get(record.id)

    provider = {
        "date": STAY,
        "price": "196",
        "price_type": "fixed",
        "reason": marked,
        "created_at": "2026-09-07T01:12:43.000Z",
        "updated_at": "2026-09-07T01:12:43.000Z",
    }

    # Every visible signal agrees...
    assert provider["reason"] == stored.reason_sent
    assert round(float(provider["price"])) == round(stored.new_price)

    # ...and ownership still fails, because the fourth check has nothing to
    # check against.
    assert check_ownership(stored, provider).owned is False

    # The automatic path never even offers it: `due` selects ACTIVE only.
    assert store.due(now=NOW + datetime.timedelta(days=30)) == []

    # And it is surfaced for a person instead.
    workload = store.workload(now=NOW + datetime.timedelta(days=30))

    assert workload["needs_review"] == 1
    assert [r["stay_date"] for r in workload["needs_attention"]] == [STAY]


def test_recorded_intent_cannot_be_rewritten_after_the_write(store):
    """Intent is what we were about to send. It is not editable afterwards."""
    record = intent(store)
    marked = build_reason(record.marker, "the real one")

    store.record_reason_sent(record.id, marked)
    store.mark_active(record.id, "2026-09-07T01:12:43.000Z", marked)

    assert not store.record_reason_sent(record.id, "something else")
    assert store.get(record.id).reason_sent == marked


# -- the confirmed lifecycle is unchanged ---------------------------------


def test_a_confirmed_write_still_reaches_active_with_its_reason(store):
    record = intent(store)
    marked = build_reason(record.marker, "because")

    store.record_reason_sent(record.id, marked)

    state = store.mark_active(
        record.id,
        "2026-09-07T01:12:43.000Z",
        marked,
        provider_updated_at="2026-09-07T01:12:43.000Z",
    )

    stored = store.get(record.id)

    assert state is CleanupState.ACTIVE
    assert stored.state == CleanupState.ACTIVE.value
    assert stored.reason_sent == marked
    assert stored.provider_created_at == "2026-09-07T01:12:43.000Z"


def test_a_confirmed_row_is_still_ownable_and_cleanable(store):
    """The normal path must be exactly as it was."""
    record = intent(store)
    marked = build_reason(record.marker, "because")

    store.record_reason_sent(record.id, marked)
    store.mark_active(
        record.id,
        "2026-09-07T01:12:43.000Z",
        marked,
        provider_updated_at="2026-09-07T01:12:43.000Z",
    )

    stored = store.get(record.id)

    provider = {
        "date": STAY,
        "price": "196",
        "price_type": "fixed",
        "reason": marked,
        "created_at": "2026-09-07T01:12:43.000Z",
        "updated_at": "2026-09-07T01:12:43.000Z",
    }

    assert check_ownership(stored, provider).owned is True
    assert [r.id for r in store.due(now=NOW + datetime.timedelta(days=1))] == [
        record.id
    ]


def test_an_override_that_was_modified_rather_than_created_still_refuses(store):
    """Recording intent did not soften the second confirming check."""
    record = intent(store)
    marked = build_reason(record.marker, "because")

    store.record_reason_sent(record.id, marked)

    state = store.mark_active(
        record.id,
        "2026-09-07T01:12:43.000Z",
        marked,
        provider_updated_at="2026-09-07T02:00:00.000Z",
    )

    assert state is CleanupState.NEEDS_REVIEW


def test_intent_is_only_recordable_while_the_write_has_not_happened(store):
    """Restricted by SQL, not by caller discipline."""
    record = intent(store)

    store.mark_active(
        record.id,
        "2026-09-07T01:12:43.000Z",
        build_reason(record.marker, "because"),
    )

    assert store.get(record.id).state == CleanupState.ACTIVE.value
    assert not store.record_reason_sent(record.id, "too late")
