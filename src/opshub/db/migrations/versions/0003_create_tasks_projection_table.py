"""Create the tasks projection table.

Revision ID: 0003_create_tasks_projection_table
Revises: 0002_create_embeddings_table
Create Date: 2026-05-17

The ``tasks`` table is the canonical read model for the task aggregate
(ADR-0002). It is fully rebuildable from the ``events`` table via
:func:`opshub.projections.rebuild.rebuild_all`; the migration provisions
the table itself, the state CHECK constraint, and a single index on
``state`` (the only filter the Phase 1 CLI needs).

State values must be one of ``draft | active | completed`` — a CHECK
constraint enforces this at the storage layer so a buggy projection
writing an unknown value surfaces as an ``IntegrityError`` rather than
corrupting the read model. The reducer in
:mod:`opshub.projections.tasks` is the only writer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_create_tasks_projection_table"
down_revision: str | Sequence[str] | None = "0002_create_embeddings_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``tasks`` projection table, its CHECK constraint and index."""
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("result_note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.CheckConstraint(
            "state IN ('draft', 'active', 'completed')",
            name=op.f("ck_tasks_state_valid"),
        ),
    )
    op.create_index(
        op.f("ix_tasks_state"),
        "tasks",
        ["state"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``tasks`` projection table (index + CHECK drop with it)."""
    op.drop_index(op.f("ix_tasks_state"), table_name="tasks")
    op.drop_table("tasks")
