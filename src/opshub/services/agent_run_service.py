"""Agent run command service (Phase 2 step 6).

:class:`AgentRunService` is the entry point for agent-run aggregate
commands from the CLI (and, later, from agent runtimes). It mirrors the
shape of :class:`~opshub.services.work_session_service.WorkSessionService`:

* Constructor takes ``store`` / ``projector`` / ``uow_factory`` / ``actor``.
* Each public command validates input, constructs the appropriate
  :class:`~opshub.domain.events.DomainEvent`, appends it to the
  :class:`EventStore`, and applies it through the :class:`Projector` —
  all inside a single Unit of Work when ``uow_factory`` is supplied.

An agent run is one execution of a named agent (``claude``, ``codex``,
...). It may be linked to a parent work session via
``work_session_id``; that link is recorded on
:class:`~opshub.domain.events.AgentRunStarted` and survives into the
``agent_runs`` projection.

``list_active`` queries the ``agent_runs`` read-model projection and
returns rows whose ``state == 'active'``. The service exposes a
value-object row (:class:`AgentRunRow`) so callers do not leak
SQLAlchemy ``Row`` mappings outside the service boundary.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid, parse_ulid_timestamp_ms
from opshub.domain.events import AgentRunEnded, AgentRunStarted
from opshub.projections.agent_runs import agent_runs_table
from opshub.services.event_store import EventStore
from opshub.services.projector import Projector

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager

    from sqlalchemy.engine import Connection, Engine

__all__ = ["AgentRunRow", "AgentRunService"]

_DEFAULT_ACTOR = "cli:agent"
_STATE_ACTIVE = "active"
_STATE_ENDED = "ended"


@dataclass(frozen=True)
class AgentRunRow:
    """Value object describing one row from the ``agent_runs`` projection.

    Returned by :meth:`AgentRunService.list_active`. Decoupling the
    consumer from the SQLAlchemy ``Row`` keeps the service boundary
    clean — the CLI / tests assert against a stable dataclass shape
    rather than a column mapping that drifts whenever the underlying
    table grows.
    """

    id: str
    agent_name: str
    work_session_id: str | None
    state: str
    started_at: datetime
    ended_at: datetime | None
    summary: str | None


def _validate_agent_name(value: str) -> None:
    """Reject empty / whitespace-only agent names."""
    if not value or not value.strip():
        raise ValidationError("agent_name must be a non-empty string")


def _validate_run_id(run_id: str) -> None:
    """Cheap ULID round-trip check for the agent run aggregate id."""
    try:
        parse_ulid_timestamp_ms(run_id)
    except ValueError as exc:
        raise ValidationError(f"invalid run_id (expected 26-char ULID): {run_id!r}") from exc


class AgentRunService:
    """Service that turns agent-run commands into appended domain events.

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
        ``"cli:agent"`` for unit tests; the CLI passes the resolved
        actor from :func:`opshub.cli._actor.resolve_owner`.
    engine:
        Optional :class:`~sqlalchemy.engine.Engine` used by
        :meth:`list_active` to read the ``agent_runs`` projection.
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

    def begin(
        self,
        agent_name: str,
        work_session_id: str | None = None,
    ) -> AgentRunStarted:
        """Begin a new agent run and return the emitted event.

        A fresh ULID is minted for ``aggregate_id`` (= the run id).
        ``work_session_id`` is optional: an agent run may exist outside
        any work session bracket (e.g. ad-hoc invocation from the CLI).

        Raises
        ------
        ValidationError
            If ``agent_name`` is empty / whitespace-only.
        """
        _validate_agent_name(agent_name)
        event = AgentRunStarted(
            aggregate_id=new_ulid(),
            actor=self._actor,
            agent_name=agent_name,
            work_session_id=work_session_id,
        )
        self._commit(event)
        return event

    def end(self, run_id: str, summary: str | None = None) -> AgentRunEnded:
        """End an active agent run, optionally recording a summary.

        Raises
        ------
        ValidationError
            If ``run_id`` is not a structurally valid 26-char ULID.
        """
        _validate_run_id(run_id)
        event = AgentRunEnded(
            aggregate_id=run_id,
            actor=self._actor,
            summary=summary,
        )
        self._commit(event)
        return event

    def list_active(self) -> list[AgentRunRow]:
        """Return every row in the ``agent_runs`` table with ``state='active'``.

        Sorted by ``started_at ASC, id ASC`` so the longest-running run
        surfaces first.
        """
        if self._engine is None:
            return []
        statement = (
            select(agent_runs_table)
            .where(agent_runs_table.c.state == _STATE_ACTIVE)
            .order_by(agent_runs_table.c.started_at.asc(), agent_runs_table.c.id.asc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(statement).mappings().all()
        return [
            AgentRunRow(
                id=row["id"],
                agent_name=row["agent_name"],
                work_session_id=row["work_session_id"],
                state=row["state"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                summary=row["summary"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ helpers

    def _commit(self, event: AgentRunStarted | AgentRunEnded) -> None:
        """Append and project inside a single Unit of Work when configured."""
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
