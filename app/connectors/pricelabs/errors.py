"""Connector errors. Nothing here carries a payload, a stack trace, or a key."""


class PriceLabsConfigurationError(Exception):
    """No PriceLabs data source is configured. Not a runtime failure."""


class PriceLabsUnavailable(Exception):
    """The source could not be reached, or answered unusably.

    Callers must treat this as *unknown* -- never as an answer, and
    specifically never as "this night is open".
    """
