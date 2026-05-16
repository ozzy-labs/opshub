"""Create the work_sessions projection table.

Revision ID: 0006_create_work_sessions_table
Revises: 0005_create_decisions_table
Create Date: 2026-05-17

The ``work_sessions`` table is the canonical read model for the work
session aggregate (Phase 2, ADR-0002). A row is created from
:class:`~opshub.domain.events.WorkSessionStarted` and transitions to
``ended`` on :class:`~opshub.domain.events.WorkSessionEnded`.

State machine: ``active`` → ``ended``. A CHECK constraint enforces both
values so a buggy projection surfaces as an ``IntegrityError`` rather
than corrupting the read model.

Indexes:

* ``ix_work_sessions_state`` — for "show every active session" lookups.
* ``ix_work_sessions_actor_started_at`` — composite index supporting
  "the sessions an actor ran, most recent first". Order matters here:
  ``(actor, started_at)`` is the right shape for ``WHERE actor = ?
  ORDER BY started_at DESC``.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_create_work_sessions_table"
down_revision: str | Sequence[str] | None = "0005_create_decisions_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``work_sessions`` projection table, CHECK and indexes."""
    op.create_table(
        "work_sessions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_sessions")),
        sa.CheckConstraint(
            "state IN ('active', 'ended')",
            name=op.f("ck_work_sessions_state_valid"),
        ),
    )
    op.create_index(
        op.f("ix_work_sessions_state"),
        "work_sessions",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_work_sessions_actor_started_at"),
        "work_sessions",
        ["actor", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``work_sessions`` table (CHECK + indexes drop with it)."""
    op.drop_index(op.f("ix_work_sessions_actor_started_at"), table_name="work_sessions")
    op.drop_index(op.f("ix_work_sessions_state"), table_name="work_sessions")
    op.drop_table("work_sessions")
