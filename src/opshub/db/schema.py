"""Shared SQLAlchemy ``MetaData`` registry and append-only tables.

This module is the source of truth for Alembic autogenerate. Every
``Table`` that lives in the OpsHub database registers on the single
:data:`metadata` instance defined here so the autogenerate diff sees the
full schema as one model (ADR-0002).

The naming convention pinned on :data:`metadata` keeps constraint /
index names deterministic across SQLite and Alembic batch operations
(see SQLAlchemy's
`naming_convention <https://docs.sqlalchemy.org/en/20/core/constraints.html#configuring-constraint-naming-conventions>`_
docs).

The :data:`events_table` definition mirrors migration
``0001_create_events_table``. We register it on the shared metadata at
import time (rather than declaring it lazily inside the event store) so
that ``alembic revision --autogenerate`` sees ``events`` symmetrically
with the read-model tables (e.g. ``tasks``). That symmetry prevents
spurious ``DROP TABLE events`` diffs.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

__all__ = ["events_table", "metadata"]

# Constraint naming convention shared by every table created via this
# MetaData. Keeping it here (rather than per-Table) lets new tables
# register without re-declaring the convention.
_NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: MetaData = MetaData(naming_convention=_NAMING_CONVENTION)


events_table: Table = Table(
    "events",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("aggregate_id", String(), nullable=False),
    Column("event_type", String(), nullable=False),
    Column("payload", Text(), nullable=False),
    Column("schema_version", Integer(), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("actor", String(), nullable=False),
    Index("ix_events_aggregate_id", "aggregate_id"),
    Index("ix_events_aggregate_id_recorded_at", "aggregate_id", "recorded_at"),
    Index("ix_events_event_type", "event_type"),
    Index("ix_events_recorded_at", "recorded_at"),
)
"""SQLAlchemy ``Table`` for the append-only ``events`` log.

The authoritative DDL lives in migration ``0001_create_events_table``;
this declaration mirrors it so that:

* :class:`~opshub.db.event_store.SqlAlchemyEventStore` can reach the
  table by name without lazy-declaring it on first use.
* ``alembic revision --autogenerate`` sees ``events`` alongside the
  read-model tables (``tasks``, ``embeddings``) — without this
  symmetry, autogenerate would emit a spurious ``DROP TABLE events``.
"""
