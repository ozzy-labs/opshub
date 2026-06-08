"""``inbox_items`` read-model projection (Phase 2, ADR-0002).

The ``inbox_items`` table is the canonical read model for the inbox
aggregate. The Phase 2 step 2 PR landed the :class:`Table` declaration
so Alembic autogenerate stays symmetric with the ``events`` / ``tasks``
tables; this module's step-3 addition is the :class:`InboxProjection`
reducer that materialises :class:`ItemEnqueued` /
:class:`ItemTriaged` events into rows.

Column shape mirrors migration ``0004_create_inbox_items_table`` (1:1).
Registering the table on the shared metadata is what makes
``alembic revision --autogenerate`` see the new projection table
symmetrically with ``events`` / ``tasks``; without it, autogenerate
would emit a spurious ``DROP TABLE inbox_items`` diff the next time
the schema evolves.

The :class:`~sqlalchemy.CheckConstraint` and indexes are repeated here
(not derived from migration) so the metadata-only callers (e.g. test
helpers that build an in-memory engine via
``metadata.create_all(engine)``) get the same shape the migration
provisions in production.
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
    text,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, ItemEnqueued, ItemTriaged

__all__ = ["InboxProjection", "inbox_items_table"]


_STATE_PENDING = "pending"
_STATE_TRIAGED_TO_TASK = "triaged_to_task"
_STATE_TRIAGED_TO_DECISION = "triaged_to_decision"
_STATE_DISCARDED = "discarded"

# Map :class:`ItemTriaged.disposition` to the resulting projection state.
# Centralising the mapping keeps the reducer linear and prevents drift
# between the event payload literals and the CHECK constraint above.
_DISPOSITION_TO_STATE: dict[str, str] = {
    "to_task": _STATE_TRIAGED_TO_TASK,
    "decision": _STATE_TRIAGED_TO_DECISION,
    "discard": _STATE_DISCARDED,
}


inbox_items_table: Table = Table(
    "inbox_items",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("summary", Text(), nullable=False),
    Column("source_ref", Text(), nullable=True),
    Column("state", Text(), nullable=False),
    Column("disposition", Text(), nullable=True),
    Column("target_id", Text(), nullable=True),
    Column("reason", Text(), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "state IN ('pending', 'triaged_to_task', 'triaged_to_decision', 'discarded')",
        name="state_valid",
    ),
    Index("ix_inbox_items_state", "state"),
    Index("ix_inbox_items_created_at", "created_at"),
    # Idempotency safety net (issue #522, ADR-0002 §"every observation is
    # a new event"). ``SourceService.observe`` mints a fresh
    # :class:`ItemEnqueued` ULID on **every** re-observation of the same
    # source, so without dedup a cursor rewind / reset / backfill (epic
    # #516) or a fingerprint-driven re-observation (``box_drive`` / ``web``)
    # would multiply inbox rows for one external item. The partial unique
    # index pins "at most one inbox row per ``source_ref``" at the storage
    # layer; the reducer's ``ON CONFLICT(source_ref) DO NOTHING`` rides on
    # it (first-observation wins). ``WHERE source_ref IS NOT NULL`` keeps
    # manual / source-less enqueues (``source_ref IS NULL``) exempt — they
    # carry no natural key to dedup on and stay one-row-per-event. Mirrors
    # migration ``0031_add_inbox_items_source_ref_unique_index``.
    Index(
        "uq_inbox_items_source_ref",
        "source_ref",
        unique=True,
        sqlite_where=text("source_ref IS NOT NULL"),
    ),
)
"""SQLAlchemy ``Table`` mirroring migration ``0004_create_inbox_items_table``.

Exported so the reducer and future query callers can compose
``select(inbox_items_table.c.id, ...)`` without re-declaring the
schema. The migration is the authoritative DDL; this declaration is
the autogenerate-symmetric mirror.

The partial unique index ``uq_inbox_items_source_ref`` (issue #522) is
declared here too so the metadata-driven schema rebuild (test helpers,
future autogenerate) sees the same dedup constraint the migration
provisions in production — mirroring the :mod:`opshub.projections.locks`
``uq_locks_active_scope`` pattern.
"""


class InboxProjection:
    """Reducer mapping inbox events to ``inbox_items`` rows.

    The reducer is a pure dispatch on event type: it issues one
    INSERT or UPDATE per event. Each statement runs on the
    Connection passed in by the caller — the projection never opens
    its own transaction (see :class:`~opshub.projections.base.Projection`).

    Event handling:

    * :class:`ItemEnqueued` → ``INSERT ... ON CONFLICT(source_ref) DO
      NOTHING`` a fresh row with ``state = 'pending'``, the summary +
      source_ref payload, and ``created_at = updated_at =
      event.occurred_at``. The conflict clause makes the apply
      **idempotent per ``source_ref``** (issue #522): a re-observation
      of an already-enqueued source — which mints a new
      :class:`ItemEnqueued` ULID every time (ADR-0002 §"every
      observation is a new event") — is dropped rather than
      duplicating the inbox row. First-observation wins; because
      :meth:`~opshub.db.event_store.SqlAlchemyEventStore.iter_all`
      replays in ``(recorded_at, id)`` order, the surviving row is
      deterministic across rebuilds. Rows with ``source_ref IS NULL``
      (manual / source-less enqueue) carry no natural key, fall outside
      the partial unique index, and so insert unconditionally — the
      historical one-row-per-event behaviour.
    * :class:`ItemTriaged` → ``UPDATE`` the row keyed by
      ``aggregate_id``, transitioning ``state`` per
      :data:`_DISPOSITION_TO_STATE`, recording ``disposition``,
      ``target_id``, ``reason``, and refreshing ``updated_at``. A
      re-observation after triage does **not** re-open the item: the
      ``DO NOTHING`` insert leaves the triaged row untouched, so a
      resolved source stays resolved (issue #522 default — a
      fingerprint-driven "re-triage this edited source" signal, if ever
      wanted, is a separate concern, not a side effect of dedup).
    * Anything else (``task.*``, ``decision.*``, coordination
      events…) is silently ignored — the rebuild driver fans every
      event out to every projection, so this projection only reacts
      to inbox-aggregate events.
    """

    name = "inbox"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``inbox_items`` row keyed by ``aggregate_id``."""
        if isinstance(event, ItemEnqueued):
            self._apply_enqueued(conn, event)
        elif isinstance(event, ItemTriaged):
            self._apply_triaged(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``inbox_items`` table.

        Issued by the rebuild driver before replay so that the projection
        is guaranteed to reflect exactly the events in the store, with no
        residue from a previous run.
        """
        conn.execute(delete(inbox_items_table))

    # ------------------------------------------------------------------ helpers

    def _apply_enqueued(self, conn: Connection, event: ItemEnqueued) -> None:
        """Insert a fresh row in the ``pending`` state, idempotent per ``source_ref``.

        ``ON CONFLICT(source_ref) DO NOTHING`` collapses re-observations
        of the same source into a single inbox row (issue #522). The
        conflict target names the partial unique index
        ``uq_inbox_items_source_ref`` via a matching ``index_where`` —
        SQLite requires the predicate to match the partial index it
        arbitrates on. Rows with ``source_ref IS NULL`` fall outside the
        index, never conflict, and so insert unconditionally (manual /
        source-less enqueue keeps its one-row-per-event semantics).
        """
        stmt = sqlite_insert(inbox_items_table).values(
            id=event.aggregate_id,
            summary=event.summary,
            source_ref=event.source_ref,
            state=_STATE_PENDING,
            disposition=None,
            target_id=None,
            reason=None,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["source_ref"],
            index_where=inbox_items_table.c.source_ref.isnot(None),
        )
        conn.execute(stmt)

    def _apply_triaged(self, conn: Connection, event: ItemTriaged) -> None:
        """Transition the matching row to its post-triage state.

        The ``disposition`` literal on the event is the source of truth
        for the new ``state`` value; the mapping table guarantees the
        result satisfies the CHECK constraint declared above.
        """
        new_state = _DISPOSITION_TO_STATE[event.disposition]
        conn.execute(
            update(inbox_items_table)
            .where(inbox_items_table.c.id == event.aggregate_id)
            .values(
                state=new_state,
                disposition=event.disposition,
                target_id=event.target_id,
                reason=event.reason,
                updated_at=event.occurred_at,
            )
        )
