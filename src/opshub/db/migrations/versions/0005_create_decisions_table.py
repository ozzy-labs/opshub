"""Create the decisions projection table.

Revision ID: 0005_create_decisions_table
Revises: 0004_create_inbox_items_table
Create Date: 2026-05-17

The ``decisions`` table is the canonical read model for the decision
aggregate (Phase 2, ADR-0002). Decisions are append-only — there is no
edit / supersede transition in Phase 2; the row reflects the single
:class:`~opshub.domain.events.DecisionRecorded` event keyed by
``aggregate_id``.

Indexes:

* ``ix_decisions_recorded_at`` — the dominant access pattern is "list
  the last N decisions in reverse chronological order".

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_create_decisions_table"
down_revision: str | Sequence[str] | None = "0004_create_inbox_items_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``decisions`` projection table and its index."""
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decisions")),
    )
    op.create_index(
        op.f("ix_decisions_recorded_at"),
        "decisions",
        ["recorded_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``decisions`` table (index drops with it)."""
    op.drop_index(op.f("ix_decisions_recorded_at"), table_name="decisions")
    op.drop_table("decisions")
