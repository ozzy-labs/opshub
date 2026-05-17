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


class ConnectorFailedError(OpsHubError):
    """A connector sync step failed against the upstream SaaS API.

    Raised by connector fetcher / mapper code when retries are
    exhausted or a non-recoverable upstream response is returned (e.g.
    repeated 401 after a forced token refresh, persistent 429 after
    backoff). The CLI driver catches it and emits ``ConnectorSyncFailed``
    with a sanitised message — connector code is therefore free to put
    upstream context (status codes, retry counts) in the message
    without worrying about leaking secrets, but MUST NOT include tokens
    or request bodies in the message text.
    """


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
    """Raised when a connector sync fails (transient or permanent).

    The connector sync service catches this and records a
    :class:`opshub.domain.events.ConnectorSyncFailed` event with a sanitised
    ``error_message`` (see ``core/sanitise``). Distinct from
    :class:`ConfigError` (which fires *before* sync starts, e.g. a missing
    token) so the CLI can map only :class:`ConnectorFailedError` onto a
    ``ConnectorSyncFailed`` event — configuration mistakes are operator
    errors, not connector failures, and should not pollute the cursor
    projection's failure record (phase-7-plan §1 #8).

    Concrete failure modes raised by connector fetchers:

    * Slack API ``invalid_auth`` (revoked / mis-scoped bot token)
    * Slack API ``channel_not_found`` / ``not_in_channel`` (configured
      channel id is wrong or the bot was kicked)
    * HTTP 429 with ``Retry-After`` after the documented retry budget
      (1s / 2s / 4s, max 3 retries per phase-7-plan §1 #8) is exhausted
    * Any transport-level error after retries are exhausted

    The error message must never contain the resolved token — connectors
    construct messages with the API ``error`` code or the exception's
    ``type(exc).__name__`` rather than echoing the raw SDK exception
    string (which can include the request body).
    """
