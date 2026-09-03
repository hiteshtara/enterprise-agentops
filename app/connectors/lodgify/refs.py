"""The safe conversation identifier the model and the browser are allowed to use.

Lodgify addresses a conversation two ways, and neither may appear in a tool
schema or an HTTP response: a numeric `booking_id` and a `thread_uid` GUID. Both
are provider identifiers, and a model that can name one can address an arbitrary
reservation.

`conversation_ref` replaces them. It is a short, deterministic digest of the
booking id -- stable across calls, so a reference returned by the list tool still
resolves when the send tool receives it, and opaque, so it carries no provider
identifier a caller could take apart and use directly.

The safety property is *not* that the digest is unguessable. It is that
resolution only ever matches a ref against bookings this account actually has:
the inbox service recomputes the digest for each real booking and looks for an
exact match. A fabricated ref matches nothing and raises, so the model cannot
reach a reservation it was never shown. The digest merely keeps the provider's
numbering out of the conversation.

No secret is encoded here, and none is needed -- see docs/LODGIFY_API.md section 6.
"""

import base64
import hashlib

# Fixed, non-secret domain separator, so a ref cannot be confused with a digest
# computed elsewhere in the system. It is not a key: see the module docstring
# for where the actual safety comes from.
REF_NAMESPACE = b"agentguard.conversation.v1"

REF_PREFIX = "PH-"

# 5 bytes -> 8 base32 characters. Comfortably collision-free across one
# account's bookings, short enough to read aloud and paste into a URL.
REF_DIGEST_BYTES = 5

REF_BODY_LENGTH = 8


def conversation_ref_for(booking_id: int) -> str:
    """The stable, opaque reference for one booking.

    Deterministic: the same booking always yields the same ref, which is what
    lets a ref survive the round trip from a list result, through the model or
    the browser, and back into a send.
    """
    digest = hashlib.blake2s(
        str(booking_id).encode("utf-8"),
        person=REF_NAMESPACE[:8],
        digest_size=REF_DIGEST_BYTES,
    ).digest()

    return REF_PREFIX + base64.b32encode(digest).decode("ascii").rstrip("=")


def is_well_formed(ref: object) -> bool:
    """Whether a value could be a ref at all.

    A cheap shape check, so an obviously malformed argument fails before any
    provider call is made. Passing says nothing about whether the ref names a
    real booking -- only resolution can answer that.
    """
    if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
        return False

    body = ref[len(REF_PREFIX) :]

    return len(body) == REF_BODY_LENGTH and body.isalnum() and body.isupper()
