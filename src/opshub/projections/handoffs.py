"""``handoffs`` read-model projection (Phase 2, ADR-0002).

The ``handoffs`` table is the canonical read model for the handoff
aggregate. The reducer (:class:`HandoffsProjection`) lands in Phase 2
step 7. The :data:`handoffs_table` :class:`~sqlalchemy.Table` is
declared at import time so it registers on the shared
:data:`opshub.db.schema.metadata` (PR #29) and the rebuild driver / CLI
wiring see the same shape the migration provisions.

Column shape mirrors migration ``0009_create_handoffs_table`` (1:1).
State transitions ``open`` → ``closed`` are enforced by the inlined
:class:`~sqlalchemy.CheckConstraint`.
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
from opshub.domain.events import DomainEvent, HandoffClosed, HandoffOpened

__all__ = ["HandoffsProjection", "handoffs_table"]


_STATE_OPEN = "open"
_STATE_CLOSED = "closed"


handoffs_table: Table = Table(
    "handoffs",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("from_actor", Text(), nullable=False),
    Column("to_actor", Text(), nullable=False),
    Column("topic", Text(), nullable=False),
    Column("state", Text(), nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column("note", Text(), nullable=True),
    CheckConstraint(
        "state IN ('open', 'closed')",
        name="state_valid",
    ),
    Index("ix_handoffs_to_actor_state", "to_actor", "state"),
    Index("ix_handoffs_opened_at", "opened_at"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0009_create_handoffs_table``."""


class HandoffsProjection:
    """Reducer mapping handoff events to ``handoffs`` rows.

    The reducer is a pure dispatch on event type:

    * :class:`HandoffOpened` → INSERT a fresh row with
      ``state='open'`` and ``opened_at = event.occurred_at``.
    * :class:`HandoffClosed` → UPDATE the matching row to
      ``state='closed'`` and stamp ``closed_at = event.occurred_at``;
      ``note`` is recorded when supplied.

    Unrecognised event types are silently ignored — the rebuild driver
    fans every event out to every projection, so this projection only
    reacts to handoff-aggregate events.
    """

    name = "handoffs"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``handoffs`` row keyed by ``aggregate_id``."""
        if isinstance(event, HandoffOpened):
            self._apply_opened(conn, event)
        elif isinstance(event, HandoffClosed):
            self._apply_closed(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``handoffs`` table.

        Issued by the rebuild driver before replay so that the
        projection is guaranteed to reflect exactly the events in the
        store, with no residue from a previous run.
        """
        conn.execute(delete(handoffs_table))

    # ------------------------------------------------------------------ helpers

    def _apply_opened(self, conn: Connection, event: HandoffOpened) -> None:
        """Insert a fresh ``handoffs`` row in the ``open`` state."""
        conn.execute(
            insert(handoffs_table).values(
                id=event.aggregate_id,
                from_actor=event.from_actor,
                to_actor=event.to_actor,
                topic=event.topic,
                state=_STATE_OPEN,
                opened_at=event.occurred_at,
                closed_at=None,
                note=None,
            )
        )

    def _apply_closed(self, conn: Connection, event: HandoffClosed) -> None:
        """Transition the matching row to ``closed`` and stamp ``closed_at``.

        ``note`` is written when supplied; we deliberately preserve the
        existing column value when ``note is None`` so a subsequent
        replay cannot wipe a note that an earlier event carried.
        """
        values: dict[str, object] = {
            "state": _STATE_CLOSED,
            "closed_at": event.occurred_at,
        }
        if event.note is not None:
            values["note"] = event.note
        conn.execute(
            update(handoffs_table).where(handoffs_table.c.id == event.aggregate_id).values(**values)
        )
