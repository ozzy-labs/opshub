"""``agent_runs`` read-model projection (Phase 2, ADR-0002).

The ``agent_runs`` table is the canonical read model for the agent run
aggregate. Phase 2 step 2 (PR #29) provisioned the
:data:`agent_runs_table`; step 6 adds the :class:`AgentRunsProjection`
reducer that materialises :class:`~opshub.domain.events.AgentRunStarted`
/ :class:`~opshub.domain.events.AgentRunEnded` into rows.

Column shape mirrors migration ``0007_create_agent_runs_table`` (1:1).
``work_session_id`` is nullable: an agent run may exist outside any
work session (e.g. ad-hoc invocation from the CLI).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
    delete,
    insert,
    update,
)
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import AgentRunEnded, AgentRunStarted, DomainEvent

__all__ = ["AgentRunsProjection", "agent_runs_table"]


_STATE_ACTIVE = "active"
_STATE_ENDED = "ended"


agent_runs_table: Table = Table(
    "agent_runs",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("agent_name", Text(), nullable=False),
    Column("work_session_id", Text(), nullable=True),
    Column("state", Text(), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("summary", Text(), nullable=True),
    CheckConstraint(
        "state IN ('active', 'ended')",
        name="state_valid",
    ),
    Index("ix_agent_runs_work_session_id_started_at", "work_session_id", "started_at"),
    Index("ix_agent_runs_agent_name", "agent_name"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0007_create_agent_runs_table``."""


class AgentRunsProjection:
    """Reducer mapping agent-run events to ``agent_runs`` rows.

    Dispatch on event type:

    * :class:`AgentRunStarted` → INSERT a row with ``state='active'``,
      ``started_at=event.occurred_at``, ``ended_at=NULL``,
      ``summary=NULL`` and ``work_session_id`` copied from the event.
    * :class:`AgentRunEnded` → UPDATE the row keyed by ``aggregate_id``
      to ``state='ended'`` and stamp ``ended_at=event.occurred_at``;
      ``summary`` is recorded when supplied.

    Unrecognised event types are silently ignored.
    """

    name = "agent_runs"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``agent_runs`` row keyed by ``aggregate_id``."""
        if isinstance(event, AgentRunStarted):
            self._apply_started(conn, event)
        elif isinstance(event, AgentRunEnded):
            self._apply_ended(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``agent_runs`` table."""
        conn.execute(delete(agent_runs_table))

    # ------------------------------------------------------------------ helpers

    def _apply_started(self, conn: Connection, event: AgentRunStarted) -> None:
        """Insert a fresh ``agent_runs`` row in the ``active`` state."""
        conn.execute(
            insert(agent_runs_table).values(
                id=event.aggregate_id,
                agent_name=event.agent_name,
                work_session_id=event.work_session_id,
                state=_STATE_ACTIVE,
                started_at=event.occurred_at,
                ended_at=None,
                summary=None,
            )
        )

    def _apply_ended(self, conn: Connection, event: AgentRunEnded) -> None:
        """Transition the matching row to ``ended`` and stamp ``ended_at``.

        ``summary`` is written when supplied; we preserve the existing
        column value when ``summary is None`` so a subsequent replay
        cannot wipe a summary that an earlier event carried.
        """
        values: dict[str, object] = {
            "state": _STATE_ENDED,
            "ended_at": event.occurred_at,
        }
        if event.summary is not None:
            values["summary"] = event.summary
        conn.execute(
            update(agent_runs_table)
            .where(agent_runs_table.c.id == event.aggregate_id)
            .values(**values)
        )
