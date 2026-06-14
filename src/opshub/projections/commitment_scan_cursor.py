"""``commitment_scan_cursor`` read-model projection (Phase 25-C, ADR-0042).

The ``commitment_scan_cursor`` table is a **singleton** checkpoint: one
row tracking how far ``opshub commitment scan`` has extracted through the
``sources`` stream, so a subsequent scan only re-reads sources observed
since the last completed scan (the LLM cost is paid once per source).

opshub has no non-connector checkpoint mechanism — the only precedent is
:mod:`opshub.projections.connector_cursors` — so this projection ports
that exact pattern (started upserts the resume-from value + start anchor;
completed advances the watermark; failed is a no-op). The single row is
keyed on the literal ``"commitment_scan"`` singleton because the scan
sweeps every connector's sources as one stream rather than per-connector.

Event handling (symmetric with ``ConnectorCursorsProjection``):

* :class:`CommitmentScanStarted` → ``INSERT ... ON CONFLICT(scan_key) DO
  UPDATE`` recording the cursor the run **resumed from** and stamping
  ``last_scanned_at`` with the run's start time (the start-of-scan
  semantic, matching ``connector_cursors.last_synced_at``).
* :class:`CommitmentScanCompleted` → ``UPDATE`` advancing ``cursor_value``
  to the new watermark + refreshing ``updated_at``. ``last_scanned_at``
  is deliberately **not** touched (it tracks the start, not completion).
* :class:`CommitmentScanFailed` → silently ignored. The watermark stays
  where the last completed scan left it so the next manual scan
  re-attempts the same un-extracted sources.

The cursor is therefore a pure function of the event log, so
``projections rebuild`` reconstructs it deterministically.

Column shape mirrors migration
``0038_create_commitment_scan_cursor_table`` (1:1).
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
    CommitmentScanCompleted,
    CommitmentScanStarted,
    DomainEvent,
)

__all__ = ["CommitmentScanCursorProjection", "commitment_scan_cursor_table"]


commitment_scan_cursor_table: Table = Table(
    "commitment_scan_cursor",
    metadata,
    Column("scan_key", Text(), primary_key=True),
    Column("cursor_value", Text(), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_scanned_at", DateTime(timezone=True), nullable=False),
)
"""SQLAlchemy ``Table`` mirroring ``0038_create_commitment_scan_cursor_table``."""


class CommitmentScanCursorProjection:
    """Reducer mapping scan-lifecycle events to the singleton cursor row.

    Threads the started / completed bracket together via the
    ``scan_key`` singleton, exactly like
    :class:`~opshub.projections.connector_cursors.ConnectorCursorsProjection`
    threads ``connector_name``. Each statement runs on the Connection
    passed in by the rebuild driver / service UoW — the projection never
    opens its own transaction.
    """

    name = "commitment_scan_cursor"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the singleton ``commitment_scan_cursor`` row."""
        if isinstance(event, CommitmentScanStarted):
            self._apply_started(conn, event)
        elif isinstance(event, CommitmentScanCompleted):
            self._apply_completed(conn, event)
        # CommitmentScanFailed (no-op) and anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``commitment_scan_cursor`` table."""
        conn.execute(delete(commitment_scan_cursor_table))

    # ------------------------------------------------------------------ helpers

    def _apply_started(self, conn: Connection, event: CommitmentScanStarted) -> None:
        """Upsert the cursor row, recording the resume-from value + start anchor."""
        stmt = sqlite_insert(commitment_scan_cursor_table).values(
            scan_key=event.aggregate_id,
            cursor_value=event.cursor_value,
            updated_at=event.occurred_at,
            last_scanned_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["scan_key"],
            set_={
                "cursor_value": stmt.excluded.cursor_value,
                "updated_at": stmt.excluded.updated_at,
                "last_scanned_at": stmt.excluded.last_scanned_at,
            },
        )
        conn.execute(stmt)

    def _apply_completed(self, conn: Connection, event: CommitmentScanCompleted) -> None:
        """Advance ``cursor_value`` (and ``updated_at`` only) for the singleton row.

        ``last_scanned_at`` is intentionally absent from the SET clause —
        it tracks the start-of-scan timestamp recorded by
        :class:`CommitmentScanStarted`.
        """
        conn.execute(
            update(commitment_scan_cursor_table)
            .where(commitment_scan_cursor_table.c.scan_key == event.aggregate_id)
            .values(
                cursor_value=event.cursor_value,
                updated_at=event.occurred_at,
            )
        )
