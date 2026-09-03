"""HTTP authentication boundary.

The only module that reads an Authorization header. Everything downstream
receives a `User` value object, so swapping this for Cognito/OIDC later changes
nothing in the domain.
"""

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status

from app.identity import Permission, PermissionDenied, User, require_permission
from app.security import InvalidToken, read_token
from app.user_store import UserStore

UNAUTHENTICATED = "Not authenticated."

INACTIVE = "This account is deactivated."

BEARER = "Bearer"


def bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization") or ""

    scheme, _, token = header.partition(" ")

    if scheme.lower() != BEARER.lower() or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHENTICATED,
            headers={"WWW-Authenticate": BEARER},
        )

    return token.strip()


def user_store(request: Request) -> UserStore:
    """Resolved from app state so tests can point at an isolated database."""
    return request.app.state.user_store


def current_user(
    token: str = Depends(bearer_token),
    store: UserStore = Depends(user_store),
) -> User:
    try:
        user_id = read_token(token)

    except InvalidToken as exc:
        # The library's reason is never echoed: a caller learns only that the
        # credential is not usable.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHENTICATED,
            headers={"WWW-Authenticate": BEARER},
        ) from exc

    user = store.get(user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHENTICATED,
            headers={"WWW-Authenticate": BEARER},
        )

    if not user.active:
        # Deactivation takes effect on the next request even if the token is
        # still cryptographically valid.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INACTIVE,
        )

    return user


def requires(permission: Permission) -> Callable[..., User]:
    """A dependency asserting one permission, returning the caller."""

    def dependency(user: User = Depends(current_user)) -> User:
        try:
            require_permission(user, permission)

        except PermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc

        return user

    return dependency


# Ready-made dependencies, built once at import rather than per route.
require_view_runs = requires(Permission.VIEW_RUNS)
require_view_audit = requires(Permission.VIEW_AUDIT)
require_view_tools = requires(Permission.VIEW_TOOLS)
require_view_approvals = requires(Permission.VIEW_APPROVALS)
require_run_agent = requires(Permission.RUN_AGENT)
require_reconcile_runs = requires(Permission.RECONCILE_RUNS)
require_administer = requires(Permission.ADMINISTER)
