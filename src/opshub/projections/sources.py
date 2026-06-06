"""``sources`` read-model projection (Phase 3, ADR-0002).

The ``sources`` table is the canonical read model for the source
aggregate. PR #45 landed the :data:`sources_table` :class:`Table`
declaration on the shared :data:`opshub.db.schema.metadata`; this
module's step-A3 addition is the :class:`SourcesProjection` reducer
that materialises :class:`SourceObserved` events into rows.

Column shape mirrors migration ``0010_create_sources_table`` (1:1),
including the :class:`~sqlalchemy.UniqueConstraint` on
``(connector_name, external_id)`` that powers the upsert semantics
required by phase-3-plan §3 機能 §3.

Upsert strategy
---------------

:class:`SourceObserved` is fundamentally a "re-observation" event: the
first time a connector sees an external item we mint a fresh source
ULID; subsequent observations of the same
``(connector_name, external_id)`` collapse into a row update so the
read model stays a single-row-per-external-item view (ADR-0010).

The reducer expresses this as a single SQLite-dialect
``INSERT ... ON CONFLICT(connector_name, external_id) DO UPDATE SET ...``
statement (via :func:`sqlalchemy.dialects.sqlite.insert`). Two reasons
to keep it as one statement rather than a SELECT-then-INSERT-or-UPDATE
pair:

* **Atomicity** — the UNIQUE constraint enforcement and the row write
  happen in the same statement, so a concurrent rebuild that hits the
  same natural key cannot interleave between the existence check and
  the write.
* **Semantics** — ``id`` (the source's ULID) and ``observed_at`` (the
  *first* observation timestamp) are deliberately omitted from the
  ``DO UPDATE SET`` clause: on conflict we keep the original values so
  references that were minted against the first-observation ULID stay
  valid. ``title`` / ``url`` / ``summary`` / ``updated_at`` /
  ``fingerprint`` are refreshed because the external item's metadata
  can drift across observations.

Phase 9 step A2 (ADR-0019 §決定 (d)) adds the ``fingerprint`` column
to back the ``box_drive`` connector's diff-detection path. The
projector writes ``event.fingerprint`` straight through — ``None``
lands as ``NULL`` so the four pre-existing connectors (``github`` /
``slack`` / ``ms365`` / ``box``), which never populate the field,
remain bit-for-bit identical in the read model.

:class:`SourceReferenced` is a deliberate no-op for this projection:
the reference graph is not stored in ``sources_table``. When a Phase 4
``links`` projection lands it will consume :class:`SourceReferenced`;
keeping it out of this reducer keeps the responsibility boundary
clean.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, SourceObserved

__all__ = ["SourcesProjection", "sources_table"]


sources_table: Table = Table(
    "sources",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("connector_name", Text(), nullable=False),
    Column("external_id", Text(), nullable=False),
    Column("source_type", Text(), nullable=False),
    Column("title", Text(), nullable=False),
    Column("url", Text(), nullable=True),
    Column("summary", Text(), nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # Phase 9 step A2 (ADR-0019 §決定 (d)): ``f"{size}:{mtime_ns}"``
    # for the ``box_drive`` FS-backed connector, ``NULL`` for the
    # four Web-API-backed connectors. Mirrors migration
    # ``0017_add_fingerprint_to_sources``.
    Column("fingerprint", String, nullable=True),
    # Phase 10 step A2 (ADR-0020 Full Local Content Retention): the
    # full retained body plus provenance tags (origin / trust). epic
    # #470 / issue #481 lifted ``body``'s ``NULL`` shim and pinned the
    # ``NOT NULL`` invariant — every connector emits a non-empty body
    # (stat-only / metadata-only paths satisfy the contract by setting
    # ``body = summary``; ADR-0010 §不変条件). Mirrors migration
    # ``0018_add_body_provenance_to_sources`` (body) +
    # ``0030_enforce_sources_body_not_null`` (the NOT NULL rebuild,
    # epic #470). ``provenance_*`` stay nullable because the
    # operator-authored workspace ingest path leaves them unset.
    Column("body", Text(), nullable=False),
    Column("provenance_origin", Text(), nullable=True),
    Column("provenance_trust", Text(), nullable=True),
    UniqueConstraint(
        "connector_name",
        "external_id",
        name="uq_sources_connector_name_external_id",
    ),
    Index("ix_sources_connector_name", "connector_name"),
    Index("ix_sources_updated_at", "updated_at"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0010_create_sources_table``."""


class SourcesProjection:
    """Reducer mapping source events to ``sources`` rows.

    The reducer is a pure dispatch on event type: it issues one
    UPSERT per :class:`SourceObserved`. Each statement runs on the
    Connection passed in by the rebuild driver — the projection never
    opens its own transaction (see :class:`~opshub.projections.base.Projection`).

    Event handling:

    * :class:`SourceObserved` → ``INSERT ... ON CONFLICT
      (connector_name, external_id) DO UPDATE`` that:

      - On INSERT: mints a row with ``id == event.aggregate_id``,
        ``observed_at == updated_at == event.occurred_at`` and the full
        title / url / summary payload.
      - On UPDATE: preserves the existing ``id`` and ``observed_at``
        (= the first-observation values) and refreshes
        ``title`` / ``url`` / ``summary`` / ``updated_at`` to reflect
        the latest observation.

    * :class:`SourceReferenced` → silently ignored. The reference graph
      will be materialised by a future ``links`` projection (Phase 4);
      storing references on the source row would duplicate that data.
    * Any other event — ignored. The rebuild driver fans every event
      out to every projection, and this reducer only owns the
      ``sources`` table.
    """

    name = "sources"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``sources`` row identified by its natural key."""
        if isinstance(event, SourceObserved):
            self._apply_observed(conn, event)
        # SourceReferenced and anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``sources`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store.
        """
        conn.execute(delete(sources_table))

    # ------------------------------------------------------------------ helpers

    def _apply_observed(self, conn: Connection, event: SourceObserved) -> None:
        """Upsert one ``sources`` row keyed by ``(connector_name, external_id)``.

        ``id`` and ``observed_at`` are intentionally absent from the
        ``set_`` mapping so the first-observation values survive
        subsequent re-observations (see module docstring).

        Phase 9 step A2: ``fingerprint`` is written through on both the
        INSERT and the UPDATE arm so the ``box_drive`` connector's
        next scan picks up the latest ``f"{size}:{mtime_ns}"`` value
        (ADR-0019 §決定 (d)). Connectors that never populate the
        field always pass ``None``, which round-trips as ``NULL`` —
        the column is nullable in migration ``0017`` precisely so the
        four pre-existing connectors stay byte-identical.

        Phase 10 step A2 (ADR-0020): ``body`` / ``provenance_origin`` /
        ``provenance_trust`` are likewise written through on both arms.
        Re-observation refreshes them so an edited upstream item
        updates the retained body and its trust tag. epic #470 / issue
        #481 promoted ``body`` to ``NOT NULL`` (stat-only / metadata-only
        connectors emit ``body = summary`` to satisfy the contract;
        ADR-0010 §不変条件 + migration
        ``0030_enforce_sources_body_not_null``). ``provenance_*`` stay
        nullable because the operator-authored workspace ingest path
        leaves them unset.
        """
        stmt = sqlite_insert(sources_table).values(
            id=event.aggregate_id,
            connector_name=event.connector_name,
            external_id=event.external_id,
            source_type=event.source_type,
            title=event.title,
            url=event.url,
            summary=event.summary,
            observed_at=event.occurred_at,
            updated_at=event.occurred_at,
            fingerprint=event.fingerprint,
            body=event.body,
            provenance_origin=event.provenance_origin,
            provenance_trust=event.provenance_trust,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["connector_name", "external_id"],
            set_={
                "title": stmt.excluded.title,
                "url": stmt.excluded.url,
                "summary": stmt.excluded.summary,
                "updated_at": stmt.excluded.updated_at,
                "fingerprint": stmt.excluded.fingerprint,
                # Phase 10 step A2 (ADR-0020): refresh body + provenance
                # on re-observation so an edited upstream item updates
                # the retained content and its trust tag.
                "body": stmt.excluded.body,
                "provenance_origin": stmt.excluded.provenance_origin,
                "provenance_trust": stmt.excluded.provenance_trust,
            },
        )
        conn.execute(stmt)
