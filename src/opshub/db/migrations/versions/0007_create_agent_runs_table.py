"""Create the agent_runs projection table.

Revision ID: 0007_create_agent_runs_table
Revises: 0006_create_work_sessions_table
Create Date: 2026-05-17

The ``agent_runs`` table is the canonical read model for the agent run
aggregate (Phase 2, ADR-0002). A row is created from
:class:`~opshub.domain.events.AgentRunStarted` and transitions to
``ended`` on :class:`~opshub.domain.events.AgentRunEnded`. Each row may
reference its parent work session via ``work_session_id`` (nullable;
agent runs can stand alone).

State machine: ``active`` → ``ended``. CHECK constraint pinned at the
storage layer.

Indexes:

* ``ix_agent_runs_work_session_id_started_at`` — composite supporting
  "all agent runs in this session, in start order". Phase 2 markdown
  rendering (step 8) walks sessions and lists their runs.
* ``ix_agent_runs_agent_name`` — "show every run of agent X" lookups.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_create_agent_runs_table"
down_revision: str | Sequence[str] | None = "0006_create_work_sessions_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``agent_runs`` projection table, CHECK and indexes."""
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("work_session_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
        sa.CheckConstraint(
            "state IN ('active', 'ended')",
            name=op.f("ck_agent_runs_state_valid"),
        ),
    )
    op.create_index(
        op.f("ix_agent_runs_work_session_id_started_at"),
        "agent_runs",
        ["work_session_id", "started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_runs_agent_name"),
        "agent_runs",
        ["agent_name"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``agent_runs`` table (CHECK + indexes drop with it)."""
    op.drop_index(op.f("ix_agent_runs_agent_name"), table_name="agent_runs")
    op.drop_index(
        op.f("ix_agent_runs_work_session_id_started_at"),
        table_name="agent_runs",
    )
    op.drop_table("agent_runs")
