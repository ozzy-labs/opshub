"""``tasks`` read-model projection.

The ``tasks`` table is the canonical read model for the task aggregate
(ADR-0002). It is fully derivable from the events stream and therefore
rebuildable from scratch via :func:`opshub.projections.rebuild.rebuild_all`.

Column shape:

* ``id`` — task ULID (= ``aggregate_id`` on every task event).
* ``title`` / ``body`` — captured at create time; the Phase 1 task events
  don't expose a rename / edit transition, so these columns are written
  once and only ever read after that.
* ``state`` — one of ``"draft" | "active" | "completed"``. Enforced by a
  CHECK constraint so that a buggy projection writing an unknown value
  surfaces at the DB layer rather than corrupting the read model.
* ``result_note`` — populated on completion.
* ``created_at`` — first :class:`~opshub.domain.events.TaskCreated`
  ``occurred_at`` (business time).
* ``updated_at`` — most recent event ``occurred_at`` applied to the row.

The :class:`TasksProjection` reducer only handles task-aggregate events;
the rebuild driver fans every event out to every projection, so unrelated
events fall through without effect.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Table,
    delete,
    insert,
    update,
)
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import (
    DomainEvent,
    TaskActivated,
    TaskCompleted,
    TaskCreated,
)

__all__ = ["TasksProjection", "tasks_table"]


_STATE_DRAFT = "draft"
_STATE_ACTIVE = "active"
_STATE_COMPLETED = "completed"


tasks_table: Table = Table(
    "tasks",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("title", String(), nullable=False),
    Column("body", String(), nullable=True),
    Column("state", String(), nullable=False),
    Column("result_note", String(), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        f"state IN ('{_STATE_DRAFT}', '{_STATE_ACTIVE}', '{_STATE_COMPLETED}')",
        name="state_valid",
    ),
    Index("ix_tasks_state", "state"),
)
"""SQLAlchemy ``Table`` for the ``tasks`` read model.

Exported so callers (CLI, future query services) can compose queries like
``select(tasks_table.c.id, tasks_table.c.state).where(...)``. The migration
in ``0003_create_tasks_projection_table`` is the authoritative DDL; this
``Table`` mirrors it and registers on the shared metadata so Alembic
autogenerate stays consistent.
"""


class TasksProjection:
    """Reducer mapping task events to ``tasks`` rows.

    The reducer is a pure dispatch on ``event_type``: it issues one
    INSERT / UPDATE per event. Each statement runs on the
    Connection passed in by the rebuild driver — the projection never
    opens its own transaction (see :class:`~opshub.projections.base.Projection`
    for the contract).

    Phase 1 only knows about task events; the reducer ignores anything
    else so the rebuild driver can safely fan every event out to every
    projection.
    """

    name = "tasks"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``tasks`` row keyed by ``aggregate_id``.

        Unrecognised event types are silently ignored — the rebuild driver
        fans every event out to every projection, so this projection only
        reacts to task-aggregate events.
        """
        if isinstance(event, TaskCreated):
            self._apply_created(conn, event)
        elif isinstance(event, TaskActivated):
            self._apply_activated(conn, event)
        elif isinstance(event, TaskCompleted):
            self._apply_completed(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``tasks`` table.

        Issued by the rebuild driver before replay so that the projection
        is guaranteed to reflect exactly the events in the store, with no
        residue from a previous run.
        """
        conn.execute(delete(tasks_table))

    # ------------------------------------------------------------------ helpers

    def _apply_created(self, conn: Connection, event: TaskCreated) -> None:
        """Insert a fresh ``tasks`` row in the ``draft`` state."""
        conn.execute(
            insert(tasks_table).values(
                id=event.aggregate_id,
                title=event.title,
                body=event.body,
                state=_STATE_DRAFT,
                result_note=None,
                created_at=event.occurred_at,
                updated_at=event.occurred_at,
            )
        )

    def _apply_activated(self, conn: Connection, event: TaskActivated) -> None:
        """Transition the matching row to ``active`` and refresh ``updated_at``."""
        conn.execute(
            update(tasks_table)
            .where(tasks_table.c.id == event.aggregate_id)
            .values(state=_STATE_ACTIVE, updated_at=event.occurred_at)
        )

    def _apply_completed(self, conn: Connection, event: TaskCompleted) -> None:
        """Transition the matching row to ``completed`` and record the note."""
        conn.execute(
            update(tasks_table)
            .where(tasks_table.c.id == event.aggregate_id)
            .values(
                state=_STATE_COMPLETED,
                result_note=event.result_note,
                updated_at=event.occurred_at,
            )
        )
