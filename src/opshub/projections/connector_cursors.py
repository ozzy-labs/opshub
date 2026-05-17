"""``connector_cursors`` read-model projection (Phase 3, ADR-0002).

The ``connector_cursors`` table tracks per-connector sync progress
(opaque cursor + ``last_synced_at`` anchor). PR #45 landed the
:data:`connector_cursors_table` :class:`Table` declaration; this
module's step-A3 addition is the :class:`ConnectorCursorsProjection`
reducer that materialises connector sync run events into rows.

Column shape mirrors migration
``0011_create_connector_cursors_table`` (1:1). ``connector_name`` is
the primary key — there is at most one row per connector — so no
secondary indexes are declared.

Lifecycle semantics
-------------------

Each ``opshub connector sync`` invocation emits three events that the
projection threads together via ``connector_name``:

* :class:`ConnectorSyncStarted` upserts the row, recording the cursor
  the run **resumed from** and stamping ``last_synced_at`` with the
  run's start time. Per phase-3-plan §4 Open Question #3,
  ``last_synced_at`` deliberately tracks the start-of-sync wall clock,
  not the end-of-sync — operators care about "when did the last sync
  attempt happen" more than "when did it finish".
* :class:`ConnectorSyncCompleted` advances ``cursor_value`` to the new
  resume token returned by the connector and refreshes ``updated_at``.
  ``last_synced_at`` is **not** touched on completion: advancing it
  would lose the start-time semantic the started event already pinned.
* :class:`ConnectorSyncFailed` is a deliberate **no-op**. The cursor
  stays at the last successful value so the next manual sync re-attempts
  from the same point and picks up a fresh diff (phase-3-plan §4 Q3
  fail-fast / retry-by-next-manual-sync stance). The failure record
  itself lives in the ``events`` table for diagnosis; we do not need a
  ``last_failed_at`` column on the cursor row.

The implementation uses SQLite's
``INSERT ... ON CONFLICT(connector_name) DO UPDATE SET ...`` (via
:func:`sqlalchemy.dialects.sqlite.insert`) so the started-event upsert
is atomic across the existence check and the row write.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Table,
    Text,
    delete,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import (
    ConnectorSyncCompleted,
    ConnectorSyncStarted,
    DomainEvent,
)

__all__ = ["ConnectorCursorsProjection", "connector_cursors_table"]


connector_cursors_table: Table = Table(
    "connector_cursors",
    metadata,
    Column("connector_name", Text(), primary_key=True),
    Column("cursor_value", Text(), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_synced_at", DateTime(timezone=True), nullable=False),
)
"""SQLAlchemy ``Table`` mirroring migration ``0011_create_connector_cursors_table``."""


class ConnectorCursorsProjection:
    """Reducer mapping connector sync run events to ``connector_cursors`` rows.

    The reducer dispatches on event type:

    * :class:`ConnectorSyncStarted` → ``INSERT ... ON CONFLICT
      (connector_name) DO UPDATE`` that refreshes ``cursor_value``,
      ``updated_at`` and ``last_synced_at`` to the started event's
      timestamp.
    * :class:`ConnectorSyncCompleted` → ``UPDATE`` keyed by
      ``connector_name`` that advances ``cursor_value`` to the post-sync
      resume token and refreshes ``updated_at``. ``last_synced_at`` is
      deliberately *not* updated — it tracks the most recent
      *start-of-sync* timestamp, not completion.
    * :class:`ConnectorSyncFailed` → silently ignored. Leaving the
      cursor where the last successful sync left it lets the next
      manual sync re-attempt from a known good point (phase-3-plan §4
      Q3).
    * Any other event — ignored. The rebuild driver fans every event
      out to every projection, and this reducer only owns the
      ``connector_cursors`` table.
    """

    name = "connector_cursors"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``connector_cursors`` row keyed by ``connector_name``."""
        if isinstance(event, ConnectorSyncStarted):
            self._apply_started(conn, event)
        elif isinstance(event, ConnectorSyncCompleted):
            self._apply_completed(conn, event)
        # ConnectorSyncFailed and anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``connector_cursors`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store.
        """
        conn.execute(delete(connector_cursors_table))

    # ------------------------------------------------------------------ helpers

    def _apply_started(self, conn: Connection, event: ConnectorSyncStarted) -> None:
        """Upsert the cursor row, recording the resume-from value and start anchor."""
        stmt = sqlite_insert(connector_cursors_table).values(
            connector_name=event.connector_name,
            cursor_value=event.cursor_value,
            updated_at=event.occurred_at,
            last_synced_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["connector_name"],
            set_={
                "cursor_value": stmt.excluded.cursor_value,
                "updated_at": stmt.excluded.updated_at,
                "last_synced_at": stmt.excluded.last_synced_at,
            },
        )
        conn.execute(stmt)

    def _apply_completed(self, conn: Connection, event: ConnectorSyncCompleted) -> None:
        """Advance ``cursor_value`` (and ``updated_at`` only) for the matching row.

        ``last_synced_at`` is intentionally absent from the SET clause:
        it tracks the start-of-sync timestamp recorded by
        :class:`ConnectorSyncStarted`. Updating it here would collapse
        the started/completed bracket and lose the "when did the last
        sync attempt begin" semantic.
        """
        conn.execute(
            update(connector_cursors_table)
            .where(connector_cursors_table.c.connector_name == event.connector_name)
            .values(
                cursor_value=event.cursor_value,
                updated_at=event.occurred_at,
            )
        )
