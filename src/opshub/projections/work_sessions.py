"""``work_sessions`` read-model projection (Phase 2, ADR-0002).

The ``work_sessions`` table is the canonical read model for the work
session aggregate. The reducer (``WorkSessionsProjection``) lands in
Phase 2 step 6; this module currently only declares the
:data:`work_sessions_table` :class:`~sqlalchemy.Table` so it registers
on the shared :data:`opshub.db.schema.metadata` at import time.

Column shape mirrors migration ``0006_create_work_sessions_table``
(1:1). State transitions ``active`` → ``ended`` are enforced by the
inlined :class:`~sqlalchemy.CheckConstraint`.
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
)

from opshub.db.schema import metadata

__all__ = ["work_sessions_table"]


work_sessions_table: Table = Table(
    "work_sessions",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("actor", Text(), nullable=False),
    Column("scope", Text(), nullable=True),
    Column("state", Text(), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("summary", Text(), nullable=True),
    CheckConstraint(
        "state IN ('active', 'ended')",
        name="state_valid",
    ),
    Index("ix_work_sessions_state", "state"),
    Index("ix_work_sessions_actor_started_at", "actor", "started_at"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0006_create_work_sessions_table``."""
