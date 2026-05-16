"""``work_sessions`` read-model projection (Phase 2, ADR-0002).

The ``work_sessions`` table is the canonical read model for the work
session aggregate. Phase 2 step 2 (PR #29) provisioned the
:data:`work_sessions_table`; step 6 adds the
:class:`WorkSessionsProjection` reducer that materialises
:class:`~opshub.domain.events.WorkSessionStarted` /
:class:`~opshub.domain.events.WorkSessionEnded` into rows.

Column shape mirrors migration ``0006_create_work_sessions_table``
(1:1). State transitions ``active`` → ``ended`` are enforced by the
inlined :class:`~sqlalchemy.CheckConstraint`.
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
from opshub.domain.events import DomainEvent, WorkSessionEnded, WorkSessionStarted

__all__ = ["WorkSessionsProjection", "work_sessions_table"]


_STATE_ACTIVE = "active"
_STATE_ENDED = "ended"


work_sessions_table: Table = Table(
    "work_sessions",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("actor", Text(), nullable=False),
    Column("scope", Text(), nullable=True),
    Column("state", Text(), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("summary", Text(), nullable=True),
    CheckConstraint(
        "state IN ('active', 'ended')",
        name="state_valid",
    ),
    Index("ix_work_sessions_state", "state"),
    Index("ix_work_sessions_actor_started_at", "actor", "started_at"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0006_create_work_sessions_table``."""


class WorkSessionsProjection:
    """Reducer mapping work-session events to ``work_sessions`` rows.

    Dispatch on event type:

    * :class:`WorkSessionStarted` → INSERT a row with
      ``state='active'``, ``started_at=event.occurred_at``,
      ``ended_at=NULL``, ``summary=NULL``.
    * :class:`WorkSessionEnded` → UPDATE the row keyed by
      ``aggregate_id`` to ``state='ended'`` and stamp
      ``ended_at=event.occurred_at``; ``summary`` is recorded when
      supplied.

    Unrecognised event types are silently ignored — the rebuild driver
    fans every event out to every projection, so this projection only
    reacts to work-session events.
    """

    name = "work_sessions"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``work_sessions`` row keyed by ``aggregate_id``."""
        if isinstance(event, WorkSessionStarted):
            self._apply_started(conn, event)
        elif isinstance(event, WorkSessionEnded):
            self._apply_ended(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``work_sessions`` table.

        Issued by the rebuild driver before replay so that the
        projection reflects exactly the events in the store.
        """
        conn.execute(delete(work_sessions_table))

    # ------------------------------------------------------------------ helpers

    def _apply_started(self, conn: Connection, event: WorkSessionStarted) -> None:
        """Insert a fresh ``work_sessions`` row in the ``active`` state."""
        conn.execute(
            insert(work_sessions_table).values(
                id=event.aggregate_id,
                actor=event.actor,
                scope=event.scope,
                state=_STATE_ACTIVE,
                started_at=event.occurred_at,
                ended_at=None,
                summary=None,
            )
        )

    def _apply_ended(self, conn: Connection, event: WorkSessionEnded) -> None:
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
            update(work_sessions_table)
            .where(work_sessions_table.c.id == event.aggregate_id)
            .values(**values)
        )
