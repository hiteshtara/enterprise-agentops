"""Read-only visibility into what cleanup owes.

The point of this endpoint is that an administrator can look at the queue
before running a pass. The point of these tests is that *looking does not
change anything* -- so most of what follows compares the whole store before
and after a read, rather than only checking the numbers are right.

Every value here is invented. Nothing in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.pricing_cleanup import (
    CleanupState,
    PricingCleanupStore,
    build_reason,
)

BUNKERS = "680444___747423"

NOW = datetime.datetime(2026, 9, 6, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def store(database) -> PricingCleanupStore:
    return PricingCleanupStore(database=database)


def written(store, stay_date, *, cleanup_at, price=164.0):
    """A row that reached the provider and is now ACTIVE."""
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=stay_date,
        old_price=182.0,
        new_price=price,
        currency="USD",
        cleanup_at=cleanup_at.isoformat(),
        approval_id="ap-1",
        run_id="run-1",
    )

    store.mark_active(
        record.id,
        provider_created_at="2026-09-06T09:00:00.000Z",
        reason_sent=build_reason(record.marker, "because"),
    )

    return store.get(record.id)


def snapshot(store):
    """Everything a read could conceivably disturb."""
    return [
        (
            r.id,
            r.state,
            r.claim_token,
            r.claimed_at,
            r.lease_until,
            r.cleanup_at,
            r.resolved_at,
            r.resolution,
        )
        for r in store.open_records()
    ]


# -- the counts -----------------------------------------------------------


def test_an_empty_queue_reports_zeroes_and_no_overdue_age(store):
    """Nothing owed is not the same as something overdue by zero hours."""
    workload = store.workload(now=NOW)

    assert workload["active"] == 0
    assert workload["due_now"] == 0
    assert workload["oldest_overdue_at"] is None
    assert workload["oldest_overdue_hours"] is None
    assert workload["needs_attention"] == []


def test_due_now_counts_only_active_rows_whose_time_has_arrived(store):
    written(store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=3))
    written(store, "2026-09-11", cleanup_at=NOW - datetime.timedelta(minutes=1))
    written(store, "2026-09-12", cleanup_at=NOW + datetime.timedelta(days=1))

    workload = store.workload(now=NOW)

    assert workload["active"] == 3
    assert workload["due_now"] == 2


def test_due_now_matches_what_a_pass_would_actually_attempt(store):
    """The number shown before a manual run is the run's candidate set.

    Asserted against `due` itself rather than re-derived, so the two cannot
    drift into disagreeing about what "arrived" means.
    """
    written(store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=3))
    written(store, "2026-09-11", cleanup_at=NOW + datetime.timedelta(days=1))

    assert store.workload(now=NOW)["due_now"] == len(store.due(now=NOW))


def test_a_pending_write_row_is_counted_but_is_not_due(store):
    """The gap between the row and the provider call is not work owed."""
    store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date="2026-09-10",
        old_price=182.0,
        new_price=164.0,
        currency="USD",
        cleanup_at=(NOW - datetime.timedelta(days=1)).isoformat(),
        approval_id="ap-1",
        run_id="run-1",
    )

    workload = store.workload(now=NOW)

    assert workload["pending_write"] == 1
    assert workload["due_now"] == 0
    # ...and it is not "overdue cleanup" either: nothing was written yet.
    assert workload["oldest_overdue_at"] is None


def test_a_claimed_row_leaves_the_due_count_and_is_reported_as_claimed(store):
    record = written(
        store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=2)
    )
    assert store.claim(record.id, "tok-1", now=NOW)

    workload = store.workload(now=NOW)

    assert workload["claimed"] == 1
    assert workload["active"] == 0
    assert workload["due_now"] == 0


def test_a_claimed_row_still_ages_as_overdue(store):
    """A row stuck under a claim is more overdue, not less.

    Dropping it from the age would make a crashed worker look like a quiet
    queue, which is the failure this number exists to catch.
    """
    record = written(
        store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=5)
    )
    assert store.claim(record.id, "tok-1", now=NOW)

    assert store.workload(now=NOW)["oldest_overdue_hours"] == 5.0


# -- the rows a person has to look at -------------------------------------


def test_a_delete_started_row_is_surfaced_with_its_night(store):
    """A count would say a person is needed without saying where."""
    record = written(
        store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=1)
    )
    assert store.claim(record.id, "tok-1", now=NOW)
    assert store.begin_delete(record.id, "tok-1")

    workload = store.workload(now=NOW)

    assert workload["delete_started"] == 1
    assert [r["stay_date"] for r in workload["needs_attention"]] == ["2026-09-10"]
    assert workload["needs_attention"][0]["state"] == (
        CleanupState.DELETE_STARTED.value
    )


def test_the_surfaced_row_carries_no_claim_token_or_lease(store):
    """Visibility, not the means to take a stuck row over.

    `DELETE_STARTED` is hands-off; handing a reader the token that fences it
    would be handing them the one thing that could un-fence it.
    """
    record = written(
        store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=1)
    )
    assert store.claim(record.id, "tok-1", now=NOW)
    assert store.begin_delete(record.id, "tok-1")

    row = store.workload(now=NOW)["needs_attention"][0]

    assert "claim_token" not in row
    assert "lease_until" not in row
    assert "claimed_at" not in row
    assert "tok-1" not in str(row)


def test_a_settled_row_is_not_workload(store):
    record = written(
        store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=1)
    )
    assert store.claim(record.id, "tok-1", now=NOW)
    assert store.begin_delete(record.id, "tok-1")
    store.resolve(
        record.id,
        CleanupState.CLEANED_UP,
        "done",
        expected_token="tok-1",
    )

    workload = store.workload(now=NOW)

    assert workload["delete_started"] == 0
    assert workload["needs_attention"] == []
    assert workload["oldest_overdue_at"] is None


# -- the property that matters --------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        CleanupState.PENDING_WRITE,
        CleanupState.ACTIVE,
        CleanupState.CLAIMED,
        CleanupState.DELETE_STARTED,
    ],
)
def test_reading_the_workload_changes_no_row_in_any_state(store, state):
    """Looking at the queue is never a way of altering it.

    Run against an *overdue* row in each state, because that is where a
    reader might plausibly be tempted to reconcile, expire a lease, or hand
    the row back to the queue. None of that may happen here.
    """
    record = written(
        store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(days=2)
    )

    if state is CleanupState.PENDING_WRITE:
        store.resolve_unclaimed(record.id, CleanupState.NEEDS_REVIEW, "x")
        record = written(
            store, "2026-09-11", cleanup_at=NOW - datetime.timedelta(days=2)
        )

    if state in (CleanupState.CLAIMED, CleanupState.DELETE_STARTED):
        assert store.claim(record.id, "tok-1", now=NOW)

        if state is CleanupState.DELETE_STARTED:
            assert store.begin_delete(record.id, "tok-1")

    before = snapshot(store)

    # Well past the lease, which is exactly when reconciliation would fire if
    # this read were doing any.
    store.workload(now=NOW + datetime.timedelta(hours=6))
    store.workload(now=NOW + datetime.timedelta(days=3))

    assert snapshot(store) == before


def test_reading_the_workload_does_not_consume_a_due_row(store):
    """Two reads in a row see the same work. It is a view, not a queue pop."""
    written(store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=1))

    assert store.workload(now=NOW)["due_now"] == 1
    assert store.workload(now=NOW)["due_now"] == 1
    assert len(store.due(now=NOW)) == 1


def test_an_expired_claim_is_not_reconciled_by_looking_at_it(store):
    """`expired_claims` sends these to a person; a read must not pre-empt it.

    If the view quietly reconciled, the fail-closed path would have already
    run by the time anyone read the number it was meant to warn them about.
    """
    record = written(
        store, "2026-09-10", cleanup_at=NOW - datetime.timedelta(hours=1)
    )
    assert store.claim(record.id, "tok-1", now=NOW)

    later = NOW + datetime.timedelta(days=1)
    store.workload(now=later)

    assert store.get(record.id).state == CleanupState.CLAIMED.value
    assert len(store.expired_claims(now=later)) == 1
