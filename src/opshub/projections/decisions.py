"""``decisions`` read-model projection (Phase 2, ADR-0002).

The ``decisions`` table is the canonical read model for the decision
aggregate. The reducer (:class:`DecisionsProjection`) consumes
:class:`~opshub.domain.events.DecisionRecorded` events and writes one row
per decision, keyed by ``aggregate_id`` (the decision's own ULID).

Decisions are append-only in Phase 2: there is no edit / supersede
transition, so each ``DecisionRecorded`` event maps to a fresh ``INSERT``
and no event re-writes an existing row. The reducer ignores any other
event type — the rebuild driver fans every event out to every projection,
so unrelated events fall through without effect.

Column shape mirrors migration ``0005_create_decisions_table`` (1:1).
The :data:`decisions_table` :class:`~sqlalchemy.Table` is registered on
the shared :data:`opshub.db.schema.metadata` at import time so Alembic
autogenerate sees it symmetrically with ``events`` / ``tasks``.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
    delete,
    insert,
)
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DecisionRecorded, DomainEvent

__all__ = ["DecisionsProjection", "decisions_table"]


decisions_table: Table = Table(
    "decisions",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("text", Text(), nullable=False),
    Column("context", Text(), nullable=True),
    Column("actor", Text(), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Index("ix_decisions_recorded_at", "recorded_at"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0005_create_decisions_table``."""


class DecisionsProjection:
    """Reducer mapping decision events to ``decisions`` rows.

    The reducer is a pure dispatch on event type: a single ``INSERT`` per
    :class:`DecisionRecorded`. Each statement runs on the Connection
    passed in by the caller (rebuild driver or service-layer UoW) — the
    projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection` for the contract).

    The reducer only knows about :class:`DecisionRecorded`; any other
    event type is silently ignored so the rebuild driver can safely fan
    every event out to every projection.
    """

    name = "decisions"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``decisions`` row keyed by ``aggregate_id``.

        Unrecognised event types are silently ignored — the rebuild
        driver fans every event out to every projection, so this
        projection only reacts to decision-aggregate events.
        """
        if isinstance(event, DecisionRecorded):
            self._apply_recorded(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``decisions`` table.

        Issued by the rebuild driver before replay so that the projection
        is guaranteed to reflect exactly the events in the store, with no
        residue from a previous run.
        """
        conn.execute(delete(decisions_table))

    # ------------------------------------------------------------------ helpers

    def _apply_recorded(self, conn: Connection, event: DecisionRecorded) -> None:
        """Insert a fresh ``decisions`` row.

        ``recorded_at`` on the row is set to ``event.occurred_at`` (the
        business time of the fact). ``event.recorded_at`` is the wall
        clock at append time and is not separately surfaced on the
        projection — the event log retains both.
        """
        conn.execute(
            insert(decisions_table).values(
                id=event.aggregate_id,
                text=event.text,
                context=event.context,
                actor=event.actor,
                recorded_at=event.occurred_at,
            )
        )
