"""Exception hierarchy for OpsHub.

Every error raised by application code should inherit from `OpsHubError` so
that CLI / service callers can distinguish project errors from stdlib
exceptions in a single `except` clause.
"""

from __future__ import annotations


class OpsHubError(Exception):
    """Base class for all OpsHub errors."""


class ConfigError(OpsHubError):
    """Configuration loading or validation failed."""


class ValidationError(OpsHubError):
    """Domain validation failed (e.g. invalid command input)."""


class NotFoundError(OpsHubError):
    """A referenced entity does not exist."""


class ConflictError(OpsHubError):
    """An operation conflicts with current state (e.g. lock already held)."""
