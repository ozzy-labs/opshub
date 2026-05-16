"""``locks`` read-model projection (Phase 2, ADR-0013).

The ``locks`` table is the canonical read model for the lock aggregate.
The reducer (``LocksProjection``) lands in Phase 2 step 5; this module
currently only declares the :data:`locks_table`
:class:`~sqlalchemy.Table` so it registers on the shared
:data:`opshub.db.schema.metadata` at import time.

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
    text,
)

from opshub.db.schema import metadata

__all__ = ["locks_table"]


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
