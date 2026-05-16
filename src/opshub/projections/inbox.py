"""``inbox_items`` read-model projection (Phase 2, ADR-0002).

The ``inbox_items`` table is the canonical read model for the inbox
aggregate. The reducer (``InboxProjection``) lands in Phase 2 step 3;
this module currently only declares the :data:`inbox_items_table`
:class:`~sqlalchemy.Table` so it registers on the shared
:data:`opshub.db.schema.metadata` at import time.

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
)

from opshub.db.schema import metadata

__all__ = ["inbox_items_table"]


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

Exported so the reducer (step 3) and future query callers can compose
``select(inbox_items_table.c.id, ...)`` without re-declaring the
schema. The migration is the authoritative DDL; this declaration is
the autogenerate-symmetric mirror.
"""
