"""Proving a webhook really came from Lodgify.

A webhook endpoint is a URL anyone on the internet can POST to. The signature is
the only thing separating a real Lodgify event from a stranger's, so it is
checked **before the body is parsed** -- not after, and never conditionally.

Lodgify's documented scheme, read from docs.lodgify.com/reference/webhooks:

  * header `ms-signature`, formatted `sha256=SIGNATURE`
  * HMAC-SHA256, the endpoint's signing secret as the key and the request body
    as the message
  * the secret is generated per endpoint and returned **only** when the
    subscription is created

Two things that look like details and are not:

  * The HMAC is over the **exact bytes received**. Re-serialising parsed JSON
    changes key order and whitespace, and the signature stops matching -- so the
    raw body is what gets hashed, and nothing upstream may consume it first.
  * The comparison is constant-time. A byte-by-byte early exit leaks how much of
    a forged signature was right, which is enough to build a valid one.

The signing secret is not the Lodgify API key. It lives in its own variable and
is never logged, audited, returned or echoed.
"""

import hashlib
import hmac
import os

SIGNATURE_HEADER = "ms-signature"

SIGNATURE_PREFIX = "sha256="

WEBHOOK_SECRET_ENV_VAR = "LODGIFY_WEBHOOK_SECRET"


class WebhookNotConfigured(Exception):
    """No signing secret is available, so nothing can be verified.

    Deliberately fatal for the request. An endpoint that accepted unverified
    events "because it is not configured yet" would be an open door.
    """


def resolve_webhook_secret() -> str:
    """Read the signing secret from the environment.

    The single seam for this credential, mirroring the API key resolver. There
    is no fallback: an unset secret makes the endpoint refuse everything rather
    than trust anything.
    """
    secret = os.environ.get(WEBHOOK_SECRET_ENV_VAR)

    if not secret:
        raise WebhookNotConfigured(
            f"{WEBHOOK_SECRET_ENV_VAR} is not set; webhook events cannot be verified."
        )

    return secret


def is_configured() -> bool:
    """Whether a signing secret exists, without reading its value."""
    return bool(os.environ.get(WEBHOOK_SECRET_ENV_VAR))


def expected_signature(body: bytes, secret: str) -> str:
    """The hex digest Lodgify should have sent for this exact body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_matches(body: bytes, header: object, secret: str) -> bool:
    """Whether the header proves this body came from Lodgify.

    Accepts the documented `sha256=` prefix and tolerates a bare digest, and is
    case-insensitive on the hex -- a signature that is right should not be
    rejected over presentation. Everything else fails closed.
    """
    if not isinstance(header, str) or not header.strip():
        return False

    candidate = header.strip()

    if candidate.lower().startswith(SIGNATURE_PREFIX):
        candidate = candidate[len(SIGNATURE_PREFIX) :]

    candidate = candidate.strip().lower()

    if not candidate:
        return False

    # compare_digest, not ==: an early-exit comparison leaks how many leading
    # characters of a forged signature were correct.
    return hmac.compare_digest(candidate, expected_signature(body, secret))
