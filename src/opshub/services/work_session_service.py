"""Work session command service (Phase 2 step 6).

:class:`WorkSessionService` is the entry point for work-session aggregate
commands from the CLI (and, later, from agent runtimes). It mirrors the
shape of :class:`~opshub.services.task_service.TaskService` and
:class:`~opshub.services.lock_service.LockService`:

* Constructor takes ``store`` / ``projector`` / ``uow_factory`` / ``actor``.
* Each public command validates input, constructs the appropriate
  :class:`~opshub.domain.events.DomainEvent`, appends it to the
  :class:`EventStore`, and applies it through the :class:`Projector` —
  all inside a single Unit of Work when ``uow_factory`` is supplied.

A work session brackets one or more :class:`agent runs
<opshub.services.agent_run_service.AgentRunService>`. The pair
(:class:`WorkSessionStarted` / :class:`WorkSessionEnded`) is the outermost
event sequence for any focused period of work; both events share the
session's ULID as ``aggregate_id``.

``list_active`` queries the ``work_sessions`` read-model projection and
returns rows whose ``state == 'active'``. The service exposes a
value-object row (:class:`WorkSessionRow`) so callers do not leak
SQLAlchemy ``Row`` mappings outside the service boundary.

``services/`` may import from ``opshub.core``, ``opshub.domain.events``,
and the read-side projection table; it must not import from
``opshub.db`` (ADR-0004 one-way dependency).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.domain.events import WorkSessionEnded, WorkSessionStarted
from opshub.projections.work_sessions import work_sessions_table
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

__all__ = ["WorkSessionRow", "WorkSessionService"]

_DEFAULT_ACTOR = "cli:session"
_STATE_ACTIVE = "active"
_STATE_ENDED = "ended"


@dataclass(frozen=True)
class WorkSessionRow:
    """Value object describing one row from the ``work_sessions`` projection.

    Returned by :meth:`WorkSessionService.list_active`. Decoupling the
    consumer from the SQLAlchemy ``Row`` keeps the service boundary
    clean — the CLI / tests assert against a stable dataclass shape
    rather than a column mapping that drifts whenever the underlying
    table grows.
    """

    id: str
    actor: str
    scope: str | None
    state: str
    started_at: datetime
    ended_at: datetime | None
    summary: str | None


def _validate_session_id(session_id: str) -> None:
    """Cheap ULID round-trip check for the work session aggregate id."""
    try:
        parse_ulid_timestamp_ms(session_id)
    except ValueError as exc:
        raise ValidationError(
            f"invalid session_id (expected 26-char ULID): {session_id!r}"
        ) from exc


class WorkSessionService:
    """Service that turns work-session commands into appended domain events.

    Parameters
    ----------
    store:
        Append target. Only the :class:`EventStore` Protocol is required.
    projector:
        Read-model updater. Called with the same event instance that
        was appended, in append order.
    uow_factory:
        Optional zero-argument callable returning a context manager
        that yields a SQLAlchemy :class:`~sqlalchemy.engine.Connection`.
        When supplied, every command runs ``store.append`` and
        ``projector.apply`` on the same connection inside the context
        manager, giving atomic append+project semantics.
    actor:
        Stamped onto every event's ``actor`` field. Defaults to
        ``"cli:session"`` for unit tests; the CLI passes the resolved
        actor from :func:`opshub.cli._actor.resolve_owner`.
    engine:
        Optional :class:`~sqlalchemy.engine.Engine` used by
        :meth:`list_active` to read the ``work_sessions`` projection.
        The CLI wiring supplies it; service unit tests can omit it and
        rely on the command path only.
    """

    def __init__(
        self,
        store: EventStore,
        projector: Projector,
        uow_factory: Callable[[], AbstractContextManager[Connection]] | None = None,
        actor: str = _DEFAULT_ACTOR,
        engine: Engine | None = None,
    ) -> None:
        self._store = store
        self._projector = projector
        self._uow_factory = uow_factory
        self._actor = actor
        self._engine = engine

    # ------------------------------------------------------------------ commands

    def start(self, scope: str | None = None) -> WorkSessionStarted:
        """Start a new work session and return the emitted event.

        A fresh ULID is minted for ``aggregate_id`` (= the session id).
        ``scope`` is an optional free-form label describing what the
        session is focused on (e.g. ``"phase-2 step 6"``).
        """
        event = WorkSessionStarted(
            aggregate_id=new_ulid(),
            actor=self._actor,
            scope=scope,
        )
        self._commit(event)
        return event

    def end(self, session_id: str, summary: str | None = None) -> WorkSessionEnded:
        """End an active work session, optionally recording a summary.

        Raises
        ------
        ValidationError
            If ``session_id`` is not a structurally valid 26-char ULID.
        """
        _validate_session_id(session_id)
        event = WorkSessionEnded(
            aggregate_id=session_id,
            actor=self._actor,
            summary=summary,
        )
        self._commit(event)
        return event

    def list_active(self) -> list[WorkSessionRow]:
        """Return every row in the ``work_sessions`` table with ``state='active'``.

        Sorted by ``started_at ASC, id ASC`` so the longest-running
        session surfaces first — useful for spotting forgotten sessions
        at the top of the list. ``id ASC`` is the deterministic
        tie-breaker when two sessions begin in the same millisecond
        (ULIDs are monotonic per millisecond).
        """
        if self._engine is None:
            # In-memory unit-test stack: the projection is unreachable.
            # Returning an empty list keeps the contract honest (the
            # caller asked for active rows; there is no store).
            return []
        statement = (
            select(work_sessions_table)
            .where(work_sessions_table.c.state == _STATE_ACTIVE)
            .order_by(work_sessions_table.c.started_at.asc(), work_sessions_table.c.id.asc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(statement).mappings().all()
        return [
            WorkSessionRow(
                id=row["id"],
                actor=row["actor"],
                scope=row["scope"],
                state=row["state"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                summary=row["summary"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ helpers

    def _commit(self, event: WorkSessionStarted | WorkSessionEnded) -> None:
        """Append and project inside a single Unit of Work when configured.

        Mirrors :meth:`TaskService._commit` exactly so failures in
        either half roll back both, keeping the event log and the
        projection table in lockstep.
        """
        with self._open_uow() as connection:
            self._store.append(event, connection)
            self._projector.apply(event, connection)

    @contextmanager
    def _open_uow(self) -> Generator[Connection | None]:
        """Yield a connection (when a UoW factory is configured) or ``None``."""
        if self._uow_factory is None:
            with nullcontext(None) as connection:
                yield connection
            return
        with self._uow_factory() as connection:
            yield connection
