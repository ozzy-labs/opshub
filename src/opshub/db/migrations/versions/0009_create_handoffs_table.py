"""Create the handoffs projection table.

Revision ID: 0009_create_handoffs_table
Revises: 0008_create_locks_table
Create Date: 2026-05-17

The ``handoffs`` table is the canonical read model for the handoff
aggregate (Phase 2, ADR-0002). A row is created from
:class:`~opshub.domain.events.HandoffOpened` and transitions to
``closed`` on :class:`~opshub.domain.events.HandoffClosed`.

State machine: ``open`` → ``closed``. CHECK constraint pinned at the
storage layer.

Indexes:

* ``ix_handoffs_to_actor_state`` — composite supporting "every open
  handoff addressed to actor X" (the inbox-style query for the
  receiving side).
* ``ix_handoffs_opened_at`` — chronological listing for markdown
  rendering (step 8) and the default ``opshub handoff list`` view.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_create_handoffs_table"
down_revision: str | Sequence[str] | None = "0008_create_locks_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``handoffs`` projection table, CHECK and indexes."""
    op.create_table(
        "handoffs",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("from_actor", sa.Text(), nullable=False),
        sa.Column("to_actor", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handoffs")),
        sa.CheckConstraint(
            "state IN ('open', 'closed')",
            name=op.f("ck_handoffs_state_valid"),
        ),
    )
    op.create_index(
        op.f("ix_handoffs_to_actor_state"),
        "handoffs",
        ["to_actor", "state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_handoffs_opened_at"),
        "handoffs",
        ["opened_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``handoffs`` table (CHECK + indexes drop with it)."""
    op.drop_index(op.f("ix_handoffs_opened_at"), table_name="handoffs")
    op.drop_index(op.f("ix_handoffs_to_actor_state"), table_name="handoffs")
    op.drop_table("handoffs")
