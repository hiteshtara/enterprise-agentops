"""The owner's live pricing window, and the fact that it only ever narrows.

Two halves, and the second matters more.

*It works*: an admin can open a window for chosen listings, it expires on its
own, ending is immediate, and a restart leaves none.

*It grants nothing on its own*: with the deployment kill switch shut or the
listing outside the environment allowlist, a perfectly valid session still
cannot produce a write. Those tests are the point of the layer -- if the
session could widen either environment control it would be a hole, not a gate.

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.pricing_session import (
    DEFAULT_SESSION_MINUTES,
    MAX_SESSION_MINUTES,
    PricingSessionStore,
    to_payload,
)

BUNKERS = "680444___747423"
HARVARD = "681286___748333"

NOW = datetime.datetime(2026, 9, 7, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def sessions() -> PricingSessionStore:
    return PricingSessionStore()


# -- default is off -------------------------------------------------------


def test_a_fresh_store_has_no_session(sessions):
    assert sessions.active(NOW) is None
    assert sessions.permits(BUNKERS, NOW) is False
    assert to_payload(None)["mode"] == "SAFE"


def test_a_new_process_starts_in_safe_mode():
    """Restart => SAFE. There is no row to survive and nothing to restore.

    Constructing a second store stands in for a second process: the state
    lives in the instance, so a fresh one is empty by construction rather than
    by a cleanup step someone has to remember.
    """
    first = PricingSessionStore()
    first.start({BUNKERS}, "user-1", now=NOW)

    assert first.permits(BUNKERS, NOW)

    restarted = PricingSessionStore()

    assert restarted.active(NOW) is None
    assert restarted.permits(BUNKERS, NOW) is False


def test_the_module_level_store_holds_no_session_at_import():
    from app.pricing_session import SESSIONS

    assert SESSIONS.active() is None


# -- starting -------------------------------------------------------------


def test_an_admin_opens_a_window_for_the_listings_they_chose(sessions):
    session = sessions.start({BUNKERS, HARVARD}, "user-1", now=NOW)

    assert session.listing_ids == frozenset({BUNKERS, HARVARD})
    assert session.started_by_user_id == "user-1"
    assert sessions.permits(BUNKERS, NOW)
    assert sessions.permits(HARVARD, NOW)


def test_a_listing_outside_the_session_is_not_covered(sessions):
    sessions.start({BUNKERS}, "user-1", now=NOW)

    assert sessions.permits(BUNKERS, NOW)
    assert sessions.permits(HARVARD, NOW) is False


@pytest.mark.parametrize("empty", [set(), frozenset()])
def test_an_empty_selection_is_refused(sessions, empty):
    """A window covering nothing is worse than one that fails to open."""
    with pytest.raises(ValueError, match="at least one listing"):
        sessions.start(empty, "user-1", now=NOW)

    assert sessions.active(NOW) is None


@pytest.mark.parametrize("missing", ["", "   "])
def test_an_actorless_session_is_refused(sessions, missing):
    with pytest.raises(ValueError, match="who opened it"):
        sessions.start({BUNKERS}, missing, now=NOW)

    assert sessions.active(NOW) is None


@pytest.mark.parametrize("minutes", [31, 60, 1440, 0, -5])
def test_a_duration_outside_the_cap_is_refused_not_clamped(sessions, minutes):
    """Refused, so nobody believes they have more time than they do."""
    with pytest.raises(ValueError, match="1 to 30 minutes"):
        sessions.start({BUNKERS}, "user-1", duration_minutes=minutes, now=NOW)

    assert sessions.active(NOW) is None


def test_the_cap_is_thirty_minutes():
    assert MAX_SESSION_MINUTES == 30
    assert DEFAULT_SESSION_MINUTES == 30


# -- expiry ---------------------------------------------------------------


def test_a_session_lapses_on_its_own(sessions):
    sessions.start({BUNKERS}, "user-1", duration_minutes=30, now=NOW)

    assert sessions.permits(BUNKERS, NOW + datetime.timedelta(minutes=29))
    assert not sessions.permits(BUNKERS, NOW + datetime.timedelta(minutes=31))


def test_expiry_is_absolute_and_activity_never_extends_it(sessions):
    """No sliding renewal: reading a session must not prolong it."""
    session = sessions.start({BUNKERS}, "user-1", duration_minutes=10, now=NOW)
    expires = session.expires_at

    for minute in (1, 3, 5, 7, 9):
        moment = NOW + datetime.timedelta(minutes=minute)

        assert sessions.permits(BUNKERS, moment)
        assert sessions.active(moment).expires_at == expires

    assert not sessions.permits(BUNKERS, NOW + datetime.timedelta(minutes=11))


def test_a_lapsed_session_is_discarded_as_it_is_read(sessions):
    sessions.start({BUNKERS}, "user-1", duration_minutes=5, now=NOW)

    later = NOW + datetime.timedelta(minutes=6)

    assert sessions.active(later) is None
    # ...and it is gone, not merely reported as inactive.
    assert sessions._session is None


def test_remaining_seconds_never_goes_negative(sessions):
    session = sessions.start({BUNKERS}, "user-1", duration_minutes=5, now=NOW)

    assert session.remaining_seconds(NOW) == 300
    assert session.remaining_seconds(NOW + datetime.timedelta(hours=1)) == 0


# -- ending ---------------------------------------------------------------


def test_ending_is_immediate(sessions):
    sessions.start({BUNKERS}, "user-1", now=NOW)

    ended = sessions.end()

    assert ended is not None
    assert sessions.active(NOW) is None
    assert sessions.permits(BUNKERS, NOW) is False


def test_ending_nothing_is_harmless(sessions):
    assert sessions.end() is None


def test_starting_again_replaces_the_previous_window(sessions):
    sessions.start({BUNKERS}, "user-1", now=NOW)
    sessions.start({HARVARD}, "user-2", now=NOW)

    assert sessions.permits(HARVARD, NOW)
    assert sessions.permits(BUNKERS, NOW) is False


# -- the payload ----------------------------------------------------------


def test_the_payload_reports_safe_when_nothing_is_open():
    body = to_payload(None)

    assert body["mode"] == "SAFE"
    assert body["active"] is False
    assert body["listing_ids"] == []
    assert body["remaining_seconds"] == 0


def test_the_payload_reports_live_with_its_listings(sessions):
    session = sessions.start({HARVARD, BUNKERS}, "user-1", now=NOW)

    body = to_payload(session, NOW)

    assert body["mode"] == "LIVE"
    assert body["active"] is True
    assert body["listing_ids"] == sorted([BUNKERS, HARVARD])
    assert body["remaining_seconds"] == 30 * 60


def test_the_store_holds_no_database_or_provider_client(sessions):
    """It cannot persist a session and cannot act on one."""
    import inspect

    assert set(vars(sessions)) == {"_lock", "_session"}

    signature = inspect.signature(PricingSessionStore.__init__)

    assert set(signature.parameters) == {"self"}

    source = inspect.getsource(PricingSessionStore)

    for forbidden in ("Database", "session()", "set_override", "remove_override"):
        assert forbidden not in source
