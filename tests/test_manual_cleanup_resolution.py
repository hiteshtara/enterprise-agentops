"""Recording that a person settled what automation refused to touch.

The 2026-09-07 incident left an obligation `NEEDS_REVIEW`: the override was
live at PriceLabs, the marker matched, and AgentGuard could not prove
ownership because the confirming re-read never arrived. Automation will never
close such a row — correctly. But nothing existed to record a person closing
it either, so the queue would carry it forever.

Two things are under test, and the second is the point:

  * a person can close a row automation gave up on; and
  * **that is all they can do.** It is bookkeeping. It cannot touch PriceLabs,
    cannot reopen a row automation still owns, cannot rewrite history, and
    must never be mistakable for "AgentGuard removed the override".

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import datetime
import inspect

import pytest

from app.pricing_cleanup import (
    MANUALLY_RESOLVABLE_STATES,
    TERMINAL_STATES,
    CleanupState,
    PricingCleanupStore,
    build_reason,
)

BUNKERS = "680444___747423"

STAY = "2026-09-08"

NOW = datetime.datetime(2026, 9, 7, 12, 0, tzinfo=datetime.UTC)

ACTOR = "user-abc"


@pytest.fixture
def store(database) -> PricingCleanupStore:
    return PricingCleanupStore(database=database)


def row(store, state: CleanupState, stay_date: str = STAY):
    """A record parked in `state`, reached the way production reaches it."""
    record = store.record_intent(
        listing_id=BUNKERS,
        pms="lodgify",
        stay_date=stay_date,
        old_price=217.0,
        new_price=196.0,
        currency="USD",
        cleanup_at=NOW.isoformat(),
        approval_id="ap-1",
        run_id="run-1",
    )

    marked = build_reason(record.marker, "because")

    if state is CleanupState.PENDING_WRITE:
        return store.get(record.id)

    if state is CleanupState.NEEDS_REVIEW:
        # The incident's own path: the confirming re-read returned nothing.
        store.record_reason_sent(record.id, marked)
        store.mark_active(record.id, None, marked)

        return store.get(record.id)

    store.mark_active(
        record.id,
        "2026-09-07T01:12:43.000Z",
        marked,
        provider_updated_at="2026-09-07T01:12:43.000Z",
    )

    if state is CleanupState.ACTIVE:
        return store.get(record.id)

    if state in (CleanupState.CLAIMED, CleanupState.DELETE_STARTED):
        store.claim(record.id, "tok-1", now=NOW)

        if state is CleanupState.DELETE_STARTED:
            store.begin_delete(record.id, "tok-1")

        return store.get(record.id)

    store.resolve(record.id, state, "settled by automation")

    return store.get(record.id)


# -- what a person may close -----------------------------------------------


@pytest.mark.parametrize(
    "state",
    [CleanupState.NEEDS_REVIEW, CleanupState.UNKNOWN_CLEANUP_STATE],
)
def test_a_row_automation_gave_up_on_can_be_closed_by_hand(store, state):
    record = row(store, state)

    assert store.record_manual_resolution(
        record.id,
        "Removed the override in the PriceLabs UI and confirmed it is gone.",
        resolved_by_user_id=ACTOR,
    )

    settled = store.get(record.id)

    assert settled.state == CleanupState.MANUALLY_RESOLVED.value
    assert settled.resolved_by_user_id == ACTOR
    assert settled.manually_resolved_at is not None
    assert "PriceLabs UI" in settled.manual_resolution


def test_the_state_never_claims_automation_removed_the_override(store):
    """`CLEANED_UP` is a claim about the worker. This must not borrow it."""
    record = row(store, CleanupState.NEEDS_REVIEW)

    store.record_manual_resolution(record.id, "done by hand", ACTOR)

    settled = store.get(record.id)

    assert settled.state == CleanupState.MANUALLY_RESOLVED.value
    assert settled.state != CleanupState.CLEANED_UP.value
    assert settled.state != CleanupState.VANISHED.value


# -- what a person may not close -------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        CleanupState.PENDING_WRITE,
        CleanupState.ACTIVE,
        CleanupState.CLAIMED,
        CleanupState.DELETE_STARTED,
        CleanupState.CLEANED_UP,
        CleanupState.VANISHED,
    ],
)
def test_a_row_still_in_play_or_already_settled_is_refused(store, state):
    """The guard is the SQL predicate, so a wrong caller still cannot win."""
    record = row(store, state)

    assert not store.record_manual_resolution(record.id, "let me through", ACTOR)

    unchanged = store.get(record.id)

    assert unchanged.state == state.value
    assert unchanged.manual_resolution is None
    assert unchanged.resolved_by_user_id is None
    assert unchanged.manually_resolved_at is None


def test_a_second_submission_is_refused_rather_than_rewriting_history(store):
    record = row(store, CleanupState.NEEDS_REVIEW)

    assert store.record_manual_resolution(record.id, "the real account", ACTOR)

    first = store.get(record.id)

    assert not store.record_manual_resolution(
        record.id,
        "a different story",
        "someone-else",
    )

    again = store.get(record.id)

    assert again.manual_resolution == first.manual_resolution
    assert again.resolved_by_user_id == ACTOR
    assert again.manually_resolved_at == first.manually_resolved_at


def test_the_permitted_set_is_exactly_the_two_given_up_states():
    assert MANUALLY_RESOLVABLE_STATES == {
        CleanupState.NEEDS_REVIEW,
        CleanupState.UNKNOWN_CLEANUP_STATE,
    }


# -- what must be supplied -------------------------------------------------


@pytest.mark.parametrize("empty", ["", "   ", "\n", "\t  \n"])
def test_an_empty_resolution_is_refused(store, empty):
    """A row closed with no explanation is not resolved, only made quiet."""
    record = row(store, CleanupState.NEEDS_REVIEW)

    with pytest.raises(ValueError, match="what was done"):
        store.record_manual_resolution(record.id, empty, ACTOR)

    assert store.get(record.id).state == CleanupState.NEEDS_REVIEW.value


@pytest.mark.parametrize("missing", ["", "   ", None])
def test_an_actor_is_required(store, missing):
    record = row(store, CleanupState.NEEDS_REVIEW)

    with pytest.raises(ValueError, match="who made it"):
        store.record_manual_resolution(record.id, "done", missing)

    assert store.get(record.id).state == CleanupState.NEEDS_REVIEW.value


# -- nothing is cleared for tidiness ---------------------------------------


def test_ownership_and_lineage_evidence_survives(store):
    """Tidying away why a row needed a person is the wrong instinct."""
    record = row(store, CleanupState.NEEDS_REVIEW)
    before = store.get(record.id)

    store.record_manual_resolution(record.id, "handled in the UI", ACTOR)

    after = store.get(record.id)

    for field in (
        "approval_id",
        "run_id",
        "reason_sent",
        "provider_created_at",
        "marker",
        "new_price",
        "old_price",
        "cleanup_at",
        "created_at",
    ):
        assert getattr(after, field) == getattr(before, field), field


def test_automations_own_verdict_is_not_overwritten(store):
    """Two voices, two sets of fields."""
    record = row(store, CleanupState.NEEDS_REVIEW)
    before = store.get(record.id)

    store.record_manual_resolution(record.id, "I removed it by hand", ACTOR)

    after = store.get(record.id)

    assert after.resolution == before.resolution
    assert after.resolved_at == before.resolved_at
    assert after.manual_resolution == "I removed it by hand"
    assert after.manual_resolution != after.resolution


# -- it cannot touch the provider ------------------------------------------


def test_the_operation_has_no_provider_access(store):
    """Absence is the safety property, not an unused attribute."""
    assert set(vars(store)) == {"_database"}
    assert "reader" not in inspect.signature(PricingCleanupStore.__init__).parameters
    assert "writer" not in inspect.signature(PricingCleanupStore.__init__).parameters

    body = inspect.getsource(PricingCleanupStore.record_manual_resolution)
    body = body.split('"""')[-1]

    for forbidden in (
        "set_override",
        "remove_override",
        "_writer",
        "_reader",
        "httpx",
        "claim(",
        "begin_delete",
    ):
        assert forbidden not in body


def test_no_claim_is_acquired(store):
    record = row(store, CleanupState.NEEDS_REVIEW)

    store.record_manual_resolution(record.id, "handled", ACTOR)

    settled = store.get(record.id)

    assert settled.claim_token is None
    assert settled.claimed_at is None
    assert settled.lease_until is None


# -- terminal, and invisible to automation ---------------------------------


def test_the_manual_state_is_terminal():
    assert CleanupState.MANUALLY_RESOLVED in TERMINAL_STATES


def test_a_manually_resolved_row_never_appears_in_due(store):
    record = row(store, CleanupState.NEEDS_REVIEW)
    store.record_manual_resolution(record.id, "handled", ACTOR)

    far_future = NOW + datetime.timedelta(days=365)

    assert store.due(now=far_future) == []
    assert store.overdue(now=far_future) == []


def test_a_manually_resolved_row_never_appears_in_expired_claims(store):
    record = row(store, CleanupState.NEEDS_REVIEW)
    store.record_manual_resolution(record.id, "handled", ACTOR)

    far_future = NOW + datetime.timedelta(days=365)

    assert store.expired_claims(now=far_future) == []


def test_a_manually_resolved_row_leaves_the_workload(store):
    """It is done. It must stop being shown as work needing anything."""
    record = row(store, CleanupState.NEEDS_REVIEW)

    before = store.workload(now=NOW)

    assert before["needs_review"] == 1
    assert len(before["needs_attention"]) == 1

    store.record_manual_resolution(record.id, "handled", ACTOR)

    after = store.workload(now=NOW)

    assert after["needs_review"] == 0
    assert after["needs_attention"] == []
    assert after["active"] == 0
    assert after["due_now"] == 0
    assert store.open_records() == []


# -- automatic cleanup is untouched ----------------------------------------


def test_automatic_cleanup_semantics_are_unchanged(store):
    """An ACTIVE row is still selected, claimable and ownable exactly as before."""
    from app.pricing_cleanup import check_ownership

    record = row(store, CleanupState.ACTIVE)

    assert [r.id for r in store.due(now=NOW + datetime.timedelta(days=1))] == [
        record.id
    ]
    assert store.claim(record.id, "tok-9", now=NOW)

    provider = {
        "date": STAY,
        "price": "196",
        "price_type": "fixed",
        "reason": store.get(record.id).reason_sent,
        "created_at": "2026-09-07T01:12:43.000Z",
        "updated_at": "2026-09-07T01:12:43.000Z",
    }

    assert check_ownership(store.get(record.id), provider).owned is True


def test_a_matching_marker_still_grants_nothing_automatically(store):
    """The incident's row must not become automatically resolvable.

    Everything visible agrees — marker, price, single-write timestamps — and
    automation still refuses. Only a person may close it, and only by saying
    so explicitly.
    """
    from app.pricing_cleanup import check_ownership, marker_of

    record = row(store, CleanupState.NEEDS_REVIEW)
    stored = store.get(record.id)

    provider = {
        "date": STAY,
        "price": "196",
        "price_type": "fixed",
        "reason": build_reason(stored.marker, "because"),
        "created_at": "2026-09-07T01:12:43.000Z",
        "updated_at": "2026-09-07T01:12:43.000Z",
    }

    assert marker_of(provider["reason"]) == record.id
    assert check_ownership(stored, provider).owned is False
    assert store.due(now=NOW + datetime.timedelta(days=365)) == []
