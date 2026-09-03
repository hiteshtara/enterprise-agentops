"""The role/permission table and the password + token primitives."""

from itertools import pairwise

import pytest

from app.authorization import ensure_can_resolve_approval, permission_for_risk
from app.identity import (
    ROLE_PERMISSIONS,
    Permission,
    PermissionDenied,
    Role,
    User,
    has_permission,
    permissions_for,
)
from app.security import (
    InvalidToken,
    hash_password,
    issue_token,
    read_token,
    verify_password,
)


def user(role: Role) -> User:
    return User(
        user_id=f"user-{role.value.lower()}",
        email=f"{role.value.lower()}@agentguard.local",
        display_name=role.value.title(),
        role=role,
        active=True,
        created_at="2026-09-02T00:00:00+00:00",
    )


# -- role / permission mapping --------------------------------------------


def test_every_role_has_an_explicit_mapping():
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_every_role_can_read():
    for role in Role:
        assert has_permission(role, Permission.VIEW_RUNS)
        assert has_permission(role, Permission.VIEW_AUDIT)
        assert has_permission(role, Permission.VIEW_TOOLS)
        assert has_permission(role, Permission.VIEW_APPROVALS)


def test_viewer_cannot_cause_anything():
    viewer = user(Role.VIEWER)

    assert not viewer.can(Permission.RUN_AGENT)
    assert not viewer.can(Permission.APPROVE_WRITE)
    assert not viewer.can(Permission.APPROVE_DANGEROUS)
    assert not viewer.can(Permission.RECONCILE_RUNS)
    assert not viewer.can(Permission.ADMINISTER)


def test_operator_can_run_but_not_approve():
    operator = user(Role.OPERATOR)

    assert operator.can(Permission.RUN_AGENT)
    assert not operator.can(Permission.APPROVE_WRITE)
    assert not operator.can(Permission.APPROVE_DANGEROUS)


def test_approver_can_release_write_but_not_dangerous():
    approver = user(Role.APPROVER)

    assert approver.can(Permission.APPROVE_WRITE)
    assert not approver.can(Permission.APPROVE_DANGEROUS)
    assert not approver.can(Permission.RECONCILE_RUNS)


def test_admin_holds_every_permission():
    assert permissions_for(Role.ADMIN) == frozenset(Permission)


def test_permission_escalates_monotonically():
    """Each role is a superset of the one below it."""
    order = [Role.VIEWER, Role.OPERATOR, Role.APPROVER, Role.ADMIN]

    for lower, higher in pairwise(order):
        assert permissions_for(lower) < permissions_for(higher)


def test_require_permission_raises_for_a_missing_permission():
    with pytest.raises(PermissionDenied, match="APPROVE_WRITE"):
        from app.identity import require_permission

        require_permission(user(Role.OPERATOR), Permission.APPROVE_WRITE)


# -- risk -> permission ----------------------------------------------------


def test_risk_maps_to_the_matching_approval_permission():
    assert permission_for_risk("WRITE") is Permission.APPROVE_WRITE
    assert permission_for_risk("DANGEROUS") is Permission.APPROVE_DANGEROUS


def test_an_unknown_risk_defaults_to_the_strictest_permission():
    """A new tier must be granted deliberately, not inherited by accident."""
    assert permission_for_risk("CATASTROPHIC") is Permission.APPROVE_DANGEROUS


def test_ensure_can_resolve_allows_and_denies_by_tier():
    ensure_can_resolve_approval(user(Role.APPROVER), "WRITE")
    ensure_can_resolve_approval(user(Role.ADMIN), "DANGEROUS")

    with pytest.raises(PermissionDenied):
        ensure_can_resolve_approval(user(Role.OPERATOR), "WRITE")

    with pytest.raises(PermissionDenied):
        ensure_can_resolve_approval(user(Role.APPROVER), "DANGEROUS")


# -- passwords -------------------------------------------------------------


def test_hashing_never_returns_the_plaintext():
    hashed = hash_password("correct-horse-battery-staple")

    assert hashed != "correct-horse-battery-staple"
    assert "correct-horse" not in hashed
    assert hashed.startswith("$2")


def test_the_same_password_hashes_differently_each_time():
    assert hash_password("same") != hash_password("same")


def test_verify_accepts_the_right_password_only():
    hashed = hash_password("right")

    assert verify_password("right", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("", hashed)


def test_verify_rejects_a_malformed_hash_without_raising():
    assert not verify_password("anything", "not-a-bcrypt-hash")
    assert not verify_password("anything", "")


def test_empty_passwords_are_refused():
    with pytest.raises(ValueError, match="must not be empty"):
        hash_password("")


# -- tokens ----------------------------------------------------------------


def test_a_token_round_trips_to_its_subject(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AUTH_SECRET", "a-secret-long-enough-for-hs256-use")

    assert read_token(issue_token("user-1")) == "user-1"


def test_a_tampered_token_is_rejected(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AUTH_SECRET", "a-secret-long-enough-for-hs256-use")

    token = issue_token("user-1")

    with pytest.raises(InvalidToken):
        read_token(token[:-2] + ("aa" if not token.endswith("aa") else "bb"))


def test_a_token_signed_with_another_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AUTH_SECRET", "first-secret-long-enough-for-hs256")

    token = issue_token("user-1")

    monkeypatch.setenv("AGENTGUARD_AUTH_SECRET", "second-secret-long-enough-for-hs25")

    with pytest.raises(InvalidToken):
        read_token(token)


def test_an_expired_token_is_rejected(monkeypatch):
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("AGENTGUARD_AUTH_SECRET", "a-secret-long-enough-for-hs256-use")

    stale = issue_token(
        "user-1",
        ttl_hours=1,
        now=datetime.now(UTC) - timedelta(hours=48),
    )

    with pytest.raises(InvalidToken):
        read_token(stale)


def test_garbage_is_rejected_without_leaking_library_detail(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AUTH_SECRET", "a-secret-long-enough-for-hs256-use")

    with pytest.raises(InvalidToken) as caught:
        read_token("not-a-token")

    assert str(caught.value) == "Invalid or expired token."


def test_the_development_fallback_secret_warns(monkeypatch):
    from app.security import auth_secret

    monkeypatch.delenv("AGENTGUARD_AUTH_SECRET", raising=False)

    with pytest.warns(RuntimeWarning, match="development-only"):
        auth_secret()
