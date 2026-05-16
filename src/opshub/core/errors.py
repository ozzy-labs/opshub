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


class OwnershipError(OpsHubError):
    """An actor attempted to mutate state owned by a different owner.

    Raised by :class:`opshub.services.lock_service.LockService.release` when
    the lock's ``(actor, work_session_id)`` owner pair does not match the
    caller's identity (ADR-0013, "Conflict semantics"). Distinct from
    :class:`ConflictError` (someone else already holds the lock at
    acquire-time) because the failure mode differs: ownership errors are
    user / agent mistakes that must surface without releasing the lock.
    """
