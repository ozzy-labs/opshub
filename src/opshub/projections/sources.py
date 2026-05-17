"""``sources`` read-model projection table (Phase 3, ADR-0002).

The ``sources`` table is the canonical read model for the source
aggregate. The reducer (:class:`SourcesProjection`) lands in Phase 3
step A3; this module ships only the :data:`sources_table`
:class:`~sqlalchemy.Table` declaration so it registers on the shared
:data:`opshub.db.schema.metadata` at import time. That symmetry keeps
``alembic revision --autogenerate`` from emitting a spurious
``DROP TABLE sources`` diff once subsequent migrations land.

Column shape mirrors migration ``0010_create_sources_table`` (1:1),
including the :class:`~sqlalchemy.UniqueConstraint` on
``(connector_name, external_id)`` that powers the upsert semantics
required by phase-3-plan §3 機能 §3.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from opshub.db.schema import metadata

__all__ = ["sources_table"]


sources_table: Table = Table(
    "sources",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("connector_name", Text(), nullable=False),
    Column("external_id", Text(), nullable=False),
    Column("source_type", Text(), nullable=False),
    Column("title", Text(), nullable=False),
    Column("url", Text(), nullable=True),
    Column("summary", Text(), nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "connector_name",
        "external_id",
        name="uq_sources_connector_name_external_id",
    ),
    Index("ix_sources_connector_name", "connector_name"),
    Index("ix_sources_updated_at", "updated_at"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0010_create_sources_table``."""
