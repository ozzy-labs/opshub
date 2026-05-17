"""``ingested_files`` read-model projection (Phase 3 step C2, ADR-0002).

The ``ingested_files`` table tracks which ``workspace/inbox/*.md``
files (by SHA-256 content hash) have already been pulled into the
event log. The :class:`opshub.services.file_ingest_service.FileIngestService`
consults this projection on every scan so an unchanged file is
skipped — the canonical idempotency story for the file ingest path
(phase-3-plan §3 機能 §5).

Column shape mirrors migration ``0012_create_ingested_files_table``
(1:1). ``content_hash`` is the primary key; one row per distinct
content body.

Upsert strategy
---------------

:class:`opshub.domain.events.file_ingest.FileIngested` always lands as
"this content was ingested, here is the file path and the originating
inbox item". The first event for a given ``content_hash`` mints the
row. Subsequent events for the same hash (e.g. a replay, or a forced
re-ingest path that bypassed the skip-by-hash guard) **refresh**
``file_path`` and ``ingested_at`` only — ``inbox_item_id`` is kept
from the first observation so external references to the originating
inbox item stay valid. This mirrors the
:class:`~opshub.projections.sources.SourcesProjection` contract where
the first-observation identity (``id`` + ``observed_at``) survives
re-observations.

The reducer expresses this as a single SQLite-dialect
``INSERT ... ON CONFLICT(content_hash) DO UPDATE SET ...`` so the
existence check and write happen in the same statement.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Table,
    Text,
    delete,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, FileIngested

__all__ = ["IngestedFilesProjection", "ingested_files_table"]


ingested_files_table: Table = Table(
    "ingested_files",
    metadata,
    Column("content_hash", Text(), primary_key=True),
    Column("file_path", Text(), nullable=False),
    Column("inbox_item_id", Text(), nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Index("ix_ingested_files_file_path", "file_path"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0012_create_ingested_files_table``."""


class IngestedFilesProjection:
    """Reducer mapping :class:`FileIngested` events to ``ingested_files`` rows.

    The reducer dispatches on event type:

    * :class:`FileIngested` → ``INSERT ... ON CONFLICT(content_hash) DO
      UPDATE`` that refreshes ``file_path`` and ``ingested_at`` while
      preserving ``inbox_item_id`` from the first observation.
    * Any other event — ignored. The rebuild driver fans every event
      out to every projection, and this reducer only owns the
      ``ingested_files`` table.
    """

    name = "ingested_files"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``ingested_files`` row keyed by ``content_hash``."""
        if isinstance(event, FileIngested):
            self._apply_ingested(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``ingested_files`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store.
        """
        conn.execute(delete(ingested_files_table))

    # ------------------------------------------------------------------ helpers

    def _apply_ingested(self, conn: Connection, event: FileIngested) -> None:
        """Upsert one ``ingested_files`` row keyed by ``content_hash``.

        ``inbox_item_id`` is intentionally absent from the
        ``DO UPDATE SET`` mapping so the first-observation inbox link
        survives subsequent re-ingest attempts of identical content.
        ``file_path`` and ``ingested_at`` refresh because the file may
        have been renamed / re-touched between observations.
        """
        stmt = sqlite_insert(ingested_files_table).values(
            content_hash=event.content_hash,
            file_path=event.file_path,
            inbox_item_id=event.inbox_item_id,
            ingested_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["content_hash"],
            set_={
                "file_path": stmt.excluded.file_path,
                "ingested_at": stmt.excluded.ingested_at,
            },
        )
        conn.execute(stmt)
