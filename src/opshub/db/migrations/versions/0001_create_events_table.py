"""Create the events table.

Revision ID: 0001_create_events_table
Revises:
Create Date: 2026-05-17

This migration provisions the authoritative append-only event store
(ADR-0002). The ``events`` table is the single source of truth in
Phase 1 — every projection is rebuildable from this table alone.

Index strategy:

* ``ix_events_aggregate_id`` — bare aggregate lookups (legacy / debug).
* ``ix_events_aggregate_id_recorded_at`` — the primary access pattern:
  "load all events for an aggregate in append order". Composite index
  beats the single-column index for ``ORDER BY recorded_at`` queries
  scoped to one aggregate.
* ``ix_events_event_type`` — type-scoped scans (e.g. all ``task.created``).
* ``ix_events_recorded_at`` — global replay / projection rebuild scans.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc. They reflect schema at a point in time, not the
current shape of the code.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_create_events_table"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``events`` table and its indexes."""
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("aggregate_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index(
        op.f("ix_events_aggregate_id"),
        "events",
        ["aggregate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_events_aggregate_id_recorded_at"),
        "events",
        ["aggregate_id", "recorded_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_events_event_type"),
        "events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_events_recorded_at"),
        "events",
        ["recorded_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``events`` table (indexes drop with it on SQLite)."""
    op.drop_index(op.f("ix_events_recorded_at"), table_name="events")
    op.drop_index(op.f("ix_events_event_type"), table_name="events")
    op.drop_index(op.f("ix_events_aggregate_id_recorded_at"), table_name="events")
    op.drop_index(op.f("ix_events_aggregate_id"), table_name="events")
    op.drop_table("events")
