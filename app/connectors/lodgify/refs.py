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


# -- enquiries -------------------------------------------------------------
#
# An enquiry is a different kind of thing from a booking conversation: it has
# its own provider id space, it is read on a separate screen, and nothing on
# its path may send. So it gets its own reference rather than borrowing
# `conversation_ref`, and the two are constructed so they can never coincide:
#
#   * a different prefix, so the strings differ even if the digests matched;
#   * a different domain separator whose first eight bytes differ from
#     `REF_NAMESPACE`'s -- blake2s only reads eight -- so booking 1001 and
#     enquiry 1001 do not produce the same digest body either.
#
# Leading with "enquiry." rather than "agentguard." is what makes the second
# property true; do not "tidy" it to match the namespace above.
ENQUIRY_REF_NAMESPACE = b"enquiry.agentguard.v1"

ENQUIRY_REF_PREFIX = "EQ-"


def enquiry_ref_for(enquiry_id: int) -> str:
    """The stable, opaque reference for one enquiry.

    Deterministic, exactly like `conversation_ref_for`: the reference the list
    route returned still resolves when the draft route receives it, without any
    server-side state between the two calls.
    """
    digest = hashlib.blake2s(
        str(enquiry_id).encode("utf-8"),
        person=ENQUIRY_REF_NAMESPACE[:8],
        digest_size=REF_DIGEST_BYTES,
    ).digest()

    return ENQUIRY_REF_PREFIX + base64.b32encode(digest).decode("ascii").rstrip("=")


def is_well_formed_enquiry_ref(ref: object) -> bool:
    """Whether a value could be an enquiry ref at all.

    A shape check only, so a malformed argument fails before any provider call.
    Passing says nothing about whether the ref names a real enquiry -- only
    resolution, which matches against enquiries this account actually has, can
    answer that.
    """
    if not isinstance(ref, str) or not ref.startswith(ENQUIRY_REF_PREFIX):
        return False

    body = ref[len(ENQUIRY_REF_PREFIX) :]

    return len(body) == REF_BODY_LENGTH and body.isalnum() and body.isupper()
