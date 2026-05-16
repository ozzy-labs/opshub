"""``decisions`` read-model projection (Phase 2, ADR-0002).

The ``decisions`` table is the canonical read model for the decision
aggregate. The reducer (``DecisionsProjection``) lands in Phase 2 step
4; this module currently only declares the :data:`decisions_table`
:class:`~sqlalchemy.Table` so it registers on the shared
:data:`opshub.db.schema.metadata` at import time.

Column shape mirrors migration ``0005_create_decisions_table`` (1:1).
Decisions are append-only: there is no state column / no transition
event in Phase 2, so the table is plain by design.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
)

from opshub.db.schema import metadata

__all__ = ["decisions_table"]


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
