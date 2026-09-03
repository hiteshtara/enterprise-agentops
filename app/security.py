"""Password hashing and bearer-token signing.

No cryptography is invented here: hashing is bcrypt, tokens are HS256 JWTs via
PyJWT. This module is the only place either library is used, so replacing the
authentication boundary later (Cognito/OIDC) touches nothing else.
"""

import os
import warnings
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

AUTH_SECRET_ENV_VAR = "AGENTGUARD_AUTH_SECRET"

# Used only when the environment supplies no secret. It is a fixed, published
# string -- tokens signed with it are forgeable by anyone reading this file --
# so it is safe for local development and unusable as a production secret.
DEVELOPMENT_ONLY_SECRET = "agentguard-development-only-insecure-secret"

BCRYPT_ROUNDS_ENV_VAR = "AGENTGUARD_BCRYPT_ROUNDS"

# bcrypt's cost factor. 12 is the production default; the only reason to lower
# it is to keep a test suite fast, and it must never be lowered in a deployed
# environment.
DEFAULT_BCRYPT_ROUNDS = 12

MIN_BCRYPT_ROUNDS = 4

MAX_BCRYPT_ROUNDS = 16

ALGORITHM = "HS256"

TOKEN_TTL_HOURS = 12

ISSUER = "agentguard"


class InvalidToken(Exception):
    """A token was missing, malformed, expired, or wrongly signed."""


def auth_secret() -> str:
    """The signing secret, warning once when the insecure fallback is used."""
    configured = os.environ.get(AUTH_SECRET_ENV_VAR)

    if configured:
        return configured

    warnings.warn(
        f"{AUTH_SECRET_ENV_VAR} is not set; using the published development-only "
        f"signing secret. Never run this configuration outside local development.",
        RuntimeWarning,
        stacklevel=2,
    )

    return DEVELOPMENT_ONLY_SECRET


def bcrypt_rounds() -> int:
    raw = os.environ.get(BCRYPT_ROUNDS_ENV_VAR)

    if not raw:
        return DEFAULT_BCRYPT_ROUNDS

    try:
        value = int(raw)

    except ValueError:
        return DEFAULT_BCRYPT_ROUNDS

    return max(MIN_BCRYPT_ROUNDS, min(value, MAX_BCRYPT_ROUNDS))


def hash_password(password: str) -> str:
    """Return a bcrypt hash. The plaintext is never stored or logged."""
    if not password:
        raise ValueError("Password must not be empty.")

    return bcrypt.hashpw(
        password.encode(),
        bcrypt.gensalt(rounds=bcrypt_rounds()),
    ).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time comparison via bcrypt. False for any malformed hash."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    except (ValueError, TypeError):
        return False


def issue_token(
    user_id: str,
    ttl_hours: int = TOKEN_TTL_HOURS,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)

    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": ISSUER,
        "iat": issued_at,
        "exp": issued_at + timedelta(hours=ttl_hours),
    }

    return jwt.encode(payload, auth_secret(), algorithm=ALGORITHM)


def read_token(token: str) -> str:
    """Return the subject (user_id) of a valid token.

    Raises InvalidToken for every failure mode. The underlying library error is
    never surfaced: a caller learns that the token is invalid, nothing more.
    """
    try:
        payload = jwt.decode(
            token,
            auth_secret(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )

    except jwt.PyJWTError as exc:
        raise InvalidToken("Invalid or expired token.") from exc

    subject = payload.get("sub")

    if not isinstance(subject, str) or not subject:
        raise InvalidToken("Invalid or expired token.")

    return subject
