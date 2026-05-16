"""Create the inbox_items projection table.

Revision ID: 0004_create_inbox_items_table
Revises: 0003_create_tasks_projection_table
Create Date: 2026-05-17

The ``inbox_items`` table is the canonical read model for the inbox
aggregate (Phase 2, ADR-0002). Rows are derived from
:class:`~opshub.domain.events.ItemEnqueued` /
:class:`~opshub.domain.events.ItemTriaged` events; the reducer lands in
step 3 — this migration only provisions the physical schema so the
event store + autogenerate stay symmetric.

State machine: ``pending`` (enqueued, awaiting triage) →
``triaged_to_task`` | ``triaged_to_decision`` | ``discarded``. A CHECK
constraint enforces the four values at the storage layer so a buggy
projection writing an unknown value surfaces as an ``IntegrityError``
rather than corrupting the read model.

Indexes:

* ``ix_inbox_items_state`` — the dominant filter (``WHERE state =
  'pending'`` for ``opshub inbox list``).
* ``ix_inbox_items_created_at`` — chronological ordering for the
  default list view and markdown rendering (step 8).

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc. They reflect schema at a point in time, not the
current shape of the code.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_create_inbox_items_table"
down_revision: str | Sequence[str] | None = "0003_create_tasks_projection_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``inbox_items`` projection table, CHECK and indexes."""
    op.create_table(
        "inbox_items",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbox_items")),
        sa.CheckConstraint(
            "state IN ('pending', 'triaged_to_task', 'triaged_to_decision', 'discarded')",
            name=op.f("ck_inbox_items_state_valid"),
        ),
    )
    op.create_index(
        op.f("ix_inbox_items_state"),
        "inbox_items",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inbox_items_created_at"),
        "inbox_items",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``inbox_items`` table (CHECK + indexes drop with it)."""
    op.drop_index(op.f("ix_inbox_items_created_at"), table_name="inbox_items")
    op.drop_index(op.f("ix_inbox_items_state"), table_name="inbox_items")
    op.drop_table("inbox_items")
