"""Provider-neutral connector errors.

Nothing here carries an HTTP body, a stack trace, or a credential. A caller
learns what class of thing went wrong and nothing about the provider's
internals.
"""


class LodgifyConfigurationError(Exception):
    """The connector is not configured (no API key). Not a runtime failure."""


class LodgifyUnavailable(Exception):
    """The provider could not be reached, or answered unusably.

    Raised for timeouts, transport errors, non-2xx responses and unparsable or
    unexpected payloads. Callers must treat this as *unknown* -- never as an
    answer, and specifically never as "available".
    """


class LodgifyRejected(Exception):
    """The provider understood the request and declined it on a business rule.

    A distinct outcome from LodgifyUnavailable: the answer is known, and it is
    "no". Carries an already-safe, human-readable reason.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason

        super().__init__(message)


class LodgifySendRefused(Exception):
    """The provider explicitly refused the send before it could take effect.

    Only raised when the provider answered with a status. Nothing was sent, so
    this outcome is safe to report as a clean failure -- see
    docs/LODGIFY_API.md section 17.
    """


class LodgifySendAmbiguous(Exception):
    """The send may or may not have happened, and we cannot tell which.

    Raised for a timeout or transport failure on the POST, where the request may
    already have left the process. The Lodgify send endpoint has no idempotency
    key, so retrying could deliver a second real message to a real guest.

    This is never converted into a failure and never triggers a retry. It
    becomes UNKNOWN_SEND_STATE and requires a human to look at the thread.
    """
