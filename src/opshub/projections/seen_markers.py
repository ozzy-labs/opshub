"""``seen_markers`` read-model projection (Phase 25-E, epic #566).

The ``seen_markers`` table is a **singleton** checkpoint: one row tracking
when the operator last ran ``opshub catchup``, so a subsequent
``catchup --since-last-seen`` only re-surfaces the diff that accrued after
that point (new sources / overdue commitments / open Slack demand).

opshub's non-connector checkpoint precedents are
:mod:`opshub.projections.connector_cursors` and
:mod:`opshub.projections.commitment_scan_cursor`; this projection ports
the same singleton-upsert pattern. The single row is keyed on the literal
``"catchup"`` singleton because the catchup sweep reads every connector's
sources + the commitment ledger + the Slack demand digest as one stream
rather than per-connector.

Event handling:

* :class:`SeenMarkerAdvanced` → ``INSERT ... ON CONFLICT(marker_key) DO
  UPDATE`` recording the new ``seen_at`` watermark + refreshing
  ``updated_at``. Last writer wins, so replaying the event log
  reconstructs the marker at the most recent advance.

The marker is therefore a pure function of the event log, so
``projections rebuild`` reconstructs it deterministically.

Column shape mirrors migration ``0039_create_seen_markers_table`` (1:1).
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Table,
    Text,
    delete,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, SeenMarkerAdvanced

__all__ = ["SeenMarkersProjection", "seen_markers_table"]


seen_markers_table: Table = Table(
    "seen_markers",
    metadata,
    Column("marker_key", Text(), primary_key=True),
    Column("seen_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
"""SQLAlchemy ``Table`` mirroring migration ``0039_create_seen_markers_table``."""


class SeenMarkersProjection:
    """Reducer mapping :class:`SeenMarkerAdvanced` to the singleton marker row.

    Each statement runs on the Connection passed in by the rebuild driver
    / service UoW — the projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection`).
    """

    name = "seen_markers"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the singleton ``seen_markers`` row."""
        if isinstance(event, SeenMarkerAdvanced):
            self._apply_advanced(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``seen_markers`` table."""
        conn.execute(delete(seen_markers_table))

    # ------------------------------------------------------------------ helpers

    def _apply_advanced(self, conn: Connection, event: SeenMarkerAdvanced) -> None:
        """Upsert the singleton marker row with the new ``seen_at`` watermark."""
        stmt = sqlite_insert(seen_markers_table).values(
            marker_key=event.aggregate_id,
            seen_at=event.seen_at,
            updated_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["marker_key"],
            set_={
                "seen_at": stmt.excluded.seen_at,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        conn.execute(stmt)
