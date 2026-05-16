"""Coordination events (Phase 2, ADR-0002, ADR-0013).

Three small aggregates live together because they coordinate work
across humans and agents:

- **Work sessions** (:class:`WorkSessionStarted` / :class:`WorkSessionEnded`):
  a bounded period during which an actor is focused on a scope. Both
  events share the work session's ULID as ``aggregate_id``.
- **Agent runs** (:class:`AgentRunStarted` / :class:`AgentRunEnded`):
  one execution of a named agent (e.g. claude, codex). An agent run may
  be linked to a parent work session via ``work_session_id``. Both
  events share the agent run's ULID as ``aggregate_id``.
- **Locks** (:class:`LockAcquired` / :class:`LockReleased`): mutual
  exclusion over a task, project, or the whole workspace (ADR-0013).
  ``LockAcquired.event_id`` doubles as the lock ULID;
  ``LockReleased.lock_id`` references it back. Both events share the
  lock's ULID as ``aggregate_id``.
"""

from __future__ import annotations

from typing import Literal

from opshub.domain.events.base import DomainEvent


class WorkSessionStarted(DomainEvent):
    """A work session began.

    ``scope`` is an optional free-form label describing what the session
    is focused on (e.g. "phase-2 events PR"). The session's ULID lives
    on ``aggregate_id``.
    """

    event_type: Literal["work_session.started"] = "work_session.started"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    scope: str | None = None


class WorkSessionEnded(DomainEvent):
    """A work session ended.

    ``summary`` is an optional wrap-up note describing what was
    accomplished. ``aggregate_id`` is the work session's ULID
    (matching the corresponding :class:`WorkSessionStarted`).
    """

    event_type: Literal["work_session.ended"] = "work_session.ended"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    summary: str | None = None


class AgentRunStarted(DomainEvent):
    """An agent began a run.

    ``agent_name`` identifies the agent (e.g. ``"claude"``, ``"codex"``).
    ``work_session_id`` optionally ties the run to a parent work
    session. ``aggregate_id`` is the agent run's ULID.
    """

    event_type: Literal["agent_run.started"] = "agent_run.started"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    agent_name: str
    work_session_id: str | None = None


class AgentRunEnded(DomainEvent):
    """An agent run ended.

    ``summary`` is an optional free-form note about the outcome.
    ``aggregate_id`` is the agent run's ULID (matching the corresponding
    :class:`AgentRunStarted`).
    """

    event_type: Literal["agent_run.ended"] = "agent_run.ended"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    summary: str | None = None


class LockAcquired(DomainEvent):
    """A lock was acquired over a task / project / the global workspace.

    ``scope_type`` selects the granularity (ADR-0013). ``scope_id`` is
    the target ULID — a task ULID, project ULID, or an empty string for
    the global scope. ``work_session_id`` optionally records which
    session is holding the lock.

    The lock's own identity is :attr:`DomainEvent.event_id`; downstream
    :class:`LockReleased` events reference it via ``lock_id``.
    ``aggregate_id`` is also set to the lock's ULID so the projector
    can group acquire/release pairs by the same key.
    """

    event_type: Literal["lock.acquired"] = "lock.acquired"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    scope_type: Literal["task", "project", "global"]
    scope_id: str
    work_session_id: str | None = None


class LockReleased(DomainEvent):
    """A previously acquired lock was released.

    ``lock_id`` references the :class:`LockAcquired` event's
    ``event_id``. ``aggregate_id`` is the same lock ULID so the
    projector can collapse acquire/release into a single row.
    """

    event_type: Literal["lock.released"] = "lock.released"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    lock_id: str
