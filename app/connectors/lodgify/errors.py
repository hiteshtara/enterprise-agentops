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
