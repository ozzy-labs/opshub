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
    insert,
    update,
)
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
)
"""SQLAlchemy ``Table`` mirroring migration ``0004_create_inbox_items_table``.

Exported so the reducer and future query callers can compose
``select(inbox_items_table.c.id, ...)`` without re-declaring the
schema. The migration is the authoritative DDL; this declaration is
the autogenerate-symmetric mirror.
"""


class InboxProjection:
    """Reducer mapping inbox events to ``inbox_items`` rows.

    The reducer is a pure dispatch on event type: it issues one
    INSERT or UPDATE per event. Each statement runs on the
    Connection passed in by the caller — the projection never opens
    its own transaction (see :class:`~opshub.projections.base.Projection`).

    Event handling:

    * :class:`ItemEnqueued` → ``INSERT`` a fresh row with ``state =
      'pending'``, the summary + source_ref payload, and
      ``created_at = updated_at = event.occurred_at``.
    * :class:`ItemTriaged` → ``UPDATE`` the row keyed by
      ``aggregate_id``, transitioning ``state`` per
      :data:`_DISPOSITION_TO_STATE`, recording ``disposition``,
      ``target_id``, ``reason``, and refreshing ``updated_at``.
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
        """Insert a fresh row in the ``pending`` state."""
        conn.execute(
            insert(inbox_items_table).values(
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
        )

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
