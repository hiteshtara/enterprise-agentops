"""The session narrows. It never widens.

The layering under test:

    writes_enabled()                      env  -- deployment kill switch
    AND listing in automation_allowlist() env  -- deployment ceiling
    AND an active session covers it       the new layer
    AND verification gates
    AND approval, fingerprint, guardrails

Every clause is an AND and the session is third, so the interesting tests are
the ones where a *valid* session still cannot produce a write. If any of those
passed, the layer would be a hole.

The write client here raises on contact, so a test that reaches the provider
fails loudly rather than silently succeeding.

Every value is invented. Nothing in this file reaches PriceLabs.
"""

import datetime

import pytest

from app.connectors.pricelabs.pricing_tools import PriceLabsPricingTools
from app.pricing_session import PricingSessionStore
from app.tool_registry import ExecutionContext

BUNKERS = "680444___747423"
HARVARD = "681286___748333"

STAY = "2026-12-20"

CTX = ExecutionContext(run_id="run-1", approval_id="ap-1")


class ExplodingProvider:
    """Any provider contact at all is a test failure."""

    def __getattr__(self, name):
        def deny(*a, **k):
            raise AssertionError(f"provider call {name!r} escaped the gate")

        return deny


@pytest.fixture
def sessions() -> PricingSessionStore:
    return PricingSessionStore()


def tools(sessions):
    return PriceLabsPricingTools(
        reader=ExplodingProvider(),
        writer=ExplodingProvider(),
        cleanups=None,
        sessions=sessions,
    )


def act(sessions, listing_id=BUNKERS, action="RAISE", price=400.0):
    return tools(sessions).apply_pricing_action(
        listing_id=listing_id,
        stay_date=STAY,
        action=action,
        proposed_price=price,
        fingerprint="whatever",
        reason="test",
        context=CTX,
    )


def live(sessions, *listings):
    sessions.start(set(listings), "user-1")


# -- the deployment ceiling wins, always ----------------------------------


def test_a_session_cannot_open_a_write_while_the_kill_switch_is_shut(
    sessions,
    monkeypatch,
):
    """The whole point of the layer, stated as a test.

    `ENABLE_PRICING_WRITES` is off and a perfectly valid session names the
    listing. The refusal must still be the deployment one, and no provider
    call may happen.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "false")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    live(sessions, BUNKERS)

    result = act(sessions)

    assert result["refusal"] == "WRITES_DISABLED"
    assert "ENABLE_PRICING_WRITES" in result["message"]


def test_a_session_cannot_bring_a_listing_past_the_env_allowlist(
    sessions,
    monkeypatch,
):
    """Writes are on, the session names Harvard, the deployment does not."""
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    live(sessions, HARVARD)

    result = act(sessions, listing_id=HARVARD)

    assert result["refusal"] == "WRITES_DISABLED"
    assert "not enabled for this listing" in result["message"]


def test_an_empty_env_allowlist_cannot_be_bypassed(sessions, monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "")

    live(sessions, BUNKERS, HARVARD)

    assert act(sessions)["refusal"] == "WRITES_DISABLED"


# -- with the deployment willing, the session decides ---------------------


def test_no_session_refuses_with_its_own_code(sessions, monkeypatch):
    """A different problem from a shut switch, so a different refusal.

    `WRITES_DISABLED` calls for an operator to change a deployment setting;
    `LIVE_SESSION_REQUIRED` calls for the owner to open a window. Collapsing
    them would send the wrong person to fix it.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    result = act(sessions)

    assert result["refusal"] == "LIVE_SESSION_REQUIRED"
    assert "pricing session" in result["message"]


def test_a_listing_outside_an_active_session_is_refused(sessions, monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv(
        "PRICELABS_AUTOMATION_ENABLED", "boston-bunkers,harvard"
    )

    live(sessions, HARVARD)

    assert act(sessions, listing_id=BUNKERS)["refusal"] == "LIVE_SESSION_REQUIRED"


def test_an_expired_session_is_refused(sessions, monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
    sessions.start({BUNKERS}, "user-1", duration_minutes=1, now=past)

    assert act(sessions)["refusal"] == "LIVE_SESSION_REQUIRED"


def test_an_ended_session_is_refused(sessions, monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    live(sessions, BUNKERS)
    sessions.end()

    assert act(sessions)["refusal"] == "LIVE_SESSION_REQUIRED"


@pytest.mark.parametrize("action", ["RAISE", "LOWER", "REMOVE_PIN"])
def test_every_owner_action_needs_a_session(sessions, monkeypatch, action):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    price = None if action == "REMOVE_PIN" else 200.0
    result = act(sessions, action=action, price=price)

    # LOWER may be refused earlier by a verification gate; either way it never
    # reaches the provider, which is what matters.
    assert result["refusal"] in {"LIVE_SESSION_REQUIRED", "UNVERIFIED_BEHAVIOUR"}


# -- the gate runs before anything with a side effect ---------------------


def test_a_refused_action_never_touches_the_provider(sessions, monkeypatch):
    """`ExplodingProvider` would raise on any contact. None of these do."""
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    for _ in range(3):
        assert act(sessions)["refusal"] == "LIVE_SESSION_REQUIRED"


def test_a_refused_action_creates_no_cleanup_obligation(sessions, monkeypatch):
    """`cleanups=None` would explode if the refusal reached `record_intent`."""
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    result = act(sessions)

    assert result["outcome"] == "CONFIRMED_FAILED"
    assert result["refusal"] == "LIVE_SESSION_REQUIRED"
    assert result["needs_human"] is False


def test_earlier_gates_still_refuse_first(sessions, monkeypatch):
    """Order is unchanged: bands, action, verification, then the switches.

    A listing with no owner bands is refused before any of it, session or not.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    live(sessions, BUNKERS)

    result = tools(sessions).apply_pricing_action(
        listing_id="not-a-listing",
        stay_date=STAY,
        action="RAISE",
        proposed_price=400.0,
        fingerprint="x",
        reason="test",
        context=CTX,
    )

    assert result["refusal"] == "NO_BANDS"


def test_an_unknown_action_is_still_refused(sessions, monkeypatch):
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "true")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    live(sessions, BUNKERS)

    result = act(sessions, action="DELETE_EVERYTHING")

    assert result["refusal"] == "INVALID_ACTION"


# -- a deployment with no session layer behaves as it did -----------------


def test_no_session_store_leaves_the_two_env_controls_as_the_whole_gate(
    monkeypatch,
):
    """`sessions=None` is the pre-session behaviour, not a bypass.

    It cannot be reached from the console: `main.py` wires a store in. It
    exists so the connector remains usable without the session layer.
    """
    monkeypatch.setenv("ENABLE_PRICING_WRITES", "false")
    monkeypatch.setenv("PRICELABS_AUTOMATION_ENABLED", "boston-bunkers")

    result = PriceLabsPricingTools(
        reader=ExplodingProvider(),
        writer=ExplodingProvider(),
        cleanups=None,
        sessions=None,
    ).apply_pricing_action(
        listing_id=BUNKERS,
        stay_date=STAY,
        action="RAISE",
        proposed_price=400.0,
        fingerprint="x",
        reason="test",
        context=CTX,
    )

    # Still refused by the deployment switch -- the session layer was never
    # what was holding it.
    assert result["refusal"] == "WRITES_DISABLED"


# -- cleanup is a different path and stays unsessioned --------------------


def test_the_cleanup_runner_does_not_consult_a_session():
    """Cleanup is restorative and runs unattended; gating it would strand
    every override the moment nobody was watching.

    Asserted structurally: the runner has no session store, and never mentions
    one.
    """
    import inspect

    from app.pricing_cleanup_runner import PricingCleanupRunner

    signature = inspect.signature(PricingCleanupRunner.__init__)

    assert "sessions" not in signature.parameters

    source = inspect.getsource(PricingCleanupRunner)

    for forbidden in ("PricingSession", "SESSIONS", "permits("):
        assert forbidden not in source


def test_cleanup_still_requires_both_environment_switches():
    """Unchanged: the runner checks them itself before the delete boundary."""
    import inspect

    from app.pricing_cleanup_runner import PricingCleanupRunner

    source = inspect.getsource(PricingCleanupRunner)

    assert "writes_enabled()" in source
    assert "bands.automation_enabled" in source
