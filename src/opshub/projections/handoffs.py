"""``handoffs`` read-model projection (Phase 2, ADR-0002).

The ``handoffs`` table is the canonical read model for the handoff
aggregate. The reducer (``HandoffsProjection``) lands in Phase 2 step
7; this module currently only declares the :data:`handoffs_table`
:class:`~sqlalchemy.Table` so it registers on the shared
:data:`opshub.db.schema.metadata` at import time.

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
)

from opshub.db.schema import metadata

__all__ = ["handoffs_table"]


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
