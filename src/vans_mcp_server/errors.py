"""Caller mistakes that must not page vans-signals."""


class ToolArgumentError(ValueError):
    """A tool or connect form rejected its own arguments.

    Subclass of ValueError so existing callers can keep catching ValueError.
    Signal forwarding matches this class by exact type, so a plain ValueError
    (decryption, a missing encryption key, a missing Google access token)
    still posts a Signal.
    """
