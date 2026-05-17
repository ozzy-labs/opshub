"""``connector_cursors`` read-model projection table (Phase 3, ADR-0002).

The ``connector_cursors`` table tracks per-connector sync progress
(opaque cursor + ``last_synced_at`` anchor). The reducer
(:class:`ConnectorCursorsProjection`) lands in Phase 3 step A3; this
module ships only the :data:`connector_cursors_table`
:class:`~sqlalchemy.Table` declaration so it registers on the shared
:data:`opshub.db.schema.metadata` at import time and Alembic
autogenerate sees it symmetrically with the other read-model tables.

Column shape mirrors migration
``0011_create_connector_cursors_table`` (1:1). ``connector_name`` is
the primary key — there is at most one row per connector — so no
secondary indexes are declared.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Table,
    Text,
)

from opshub.db.schema import metadata

__all__ = ["connector_cursors_table"]


connector_cursors_table: Table = Table(
    "connector_cursors",
    metadata,
    Column("connector_name", Text(), primary_key=True),
    Column("cursor_value", Text(), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_synced_at", DateTime(timezone=True), nullable=False),
)
"""SQLAlchemy ``Table`` mirroring migration ``0011_create_connector_cursors_table``."""
