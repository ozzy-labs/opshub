"""``locks`` read-model projection (Phase 2, ADR-0013).

The ``locks`` table is the canonical read model for the lock aggregate.
Phase 2 step 2 (PR #29) provisioned the :data:`locks_table` and its
partial unique index ``uq_locks_active_scope``; step 5 adds the
:class:`LocksProjection` reducer that materialises
:class:`~opshub.domain.events.LockAcquired` /
:class:`~opshub.domain.events.LockReleased` into rows.

Column shape mirrors migration ``0008_create_locks_table`` (1:1). The
two indexes — including the **partial unique index**
``uq_locks_active_scope`` that pins "at most one active lock per
``(scope_type, scope_id)``" at the storage layer (ADR-0013) — are
repeated here via :class:`~sqlalchemy.Index` with ``sqlite_where`` so
the metadata-driven schema rebuild (test helpers, future autogenerate)
sees the same constraint the migration provisions in production.
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
    text,
    update,
)
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, LockAcquired, LockReleased

__all__ = ["LocksProjection", "locks_table"]


locks_table: Table = Table(
    "locks",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("scope_type", Text(), nullable=False),
    Column("scope_id", Text(), nullable=False),
    Column("actor", Text(), nullable=False),
    Column("work_session_id", Text(), nullable=True),
    Column("acquired_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "scope_type IN ('task', 'project', 'global')",
        name="scope_type_valid",
    ),
    Index(
        "uq_locks_active_scope",
        "scope_type",
        "scope_id",
        unique=True,
        sqlite_where=text("released_at IS NULL"),
    ),
    Index("ix_locks_actor_acquired_at", "actor", "acquired_at"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0008_create_locks_table``.

The partial unique index is the safety net behind
:class:`~opshub.services.lock_service.LockService.acquire` (step 5):
two concurrent acquires racing through the projection on the same
``(scope_type, scope_id)`` while both ``released_at`` values are
``NULL`` will fail at INSERT with an ``IntegrityError`` instead of
silently double-booking the lock (ADR-0013, fail-fast semantics).
"""


class LocksProjection:
    """Reducer mapping lock events to ``locks`` rows.

    The reducer dispatches on event type:

    * :class:`LockAcquired` → INSERT a fresh row with
      ``id == aggregate_id`` (= the lock's ULID), copying scope / actor /
      session and stamping ``acquired_at = event.occurred_at``.
    * :class:`LockReleased` → UPDATE the row keyed by ``aggregate_id``
      to set ``released_at = event.occurred_at``. A
      :class:`LockReleased` whose ``aggregate_id`` does not match an
      existing row is silently a no-op — the rebuild driver fans every
      event out to every projection, and a missing row is invariably a
      symptom that the earlier :class:`LockAcquired` was filtered out
      upstream (e.g. dropped event).
    * Any other event — ignored (see :class:`Projection` for the contract).

    Each statement runs on the connection threaded in by the
    :class:`~opshub.services.lock_service.LockService` Unit of Work; the
    projection never opens its own transaction.
    """

    name = "locks"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``locks`` row keyed by ``aggregate_id``."""
        if isinstance(event, LockAcquired):
            self._apply_acquired(conn, event)
        elif isinstance(event, LockReleased):
            self._apply_released(conn, event)
        # Anything else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``locks`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store. Releases
        that arrive without a matching acquire stay a no-op after
        ``reset``, which keeps the rebuild deterministic.
        """
        conn.execute(delete(locks_table))

    # ------------------------------------------------------------------ helpers

    def _apply_acquired(self, conn: Connection, event: LockAcquired) -> None:
        """Insert a row representing the active lock."""
        conn.execute(
            insert(locks_table).values(
                id=event.aggregate_id,
                scope_type=event.scope_type,
                scope_id=event.scope_id,
                actor=event.actor,
                work_session_id=event.work_session_id,
                acquired_at=event.occurred_at,
                released_at=None,
            )
        )

    def _apply_released(self, conn: Connection, event: LockReleased) -> None:
        """Stamp ``released_at`` on the matching row."""
        conn.execute(
            update(locks_table)
            .where(locks_table.c.id == event.aggregate_id)
            .values(released_at=event.occurred_at)
        )
