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


class ConnectorFailedError(OpsHubError):
    """A connector aborted a sync because the external SaaS surface failed.

    Connectors raise this when they have exhausted their fallback paths —
    e.g. the auth token cannot be refreshed (repeated 401), the API keeps
    rate-limiting after the configured retry budget (repeated 429), or a
    response shape drifts so far that the connector cannot parse it. The
    CLI driver in :mod:`opshub.services.connector_sync_service` translates
    this exception into a ``ConnectorSyncFailed`` event (ADR-0010) so the
    failure is durably recorded alongside the run that produced it.

    The message is operator-actionable but must NOT echo bearer tokens or
    raw response bodies that may contain user data — connectors are
    responsible for sanitising before raising (ADR-0005 External Content
    Min).
    """
