"""``agent_runs`` read-model projection (Phase 2, ADR-0002).

The ``agent_runs`` table is the canonical read model for the agent run
aggregate. The reducer (``AgentRunsProjection``) lands in Phase 2 step
6; this module currently only declares the :data:`agent_runs_table`
:class:`~sqlalchemy.Table` so it registers on the shared
:data:`opshub.db.schema.metadata` at import time.

Column shape mirrors migration ``0007_create_agent_runs_table`` (1:1).
``work_session_id`` is nullable: an agent run may exist outside any
work session (e.g. ad-hoc invocation from the CLI).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
)

from opshub.db.schema import metadata

__all__ = ["agent_runs_table"]


agent_runs_table: Table = Table(
    "agent_runs",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("agent_name", Text(), nullable=False),
    Column("work_session_id", Text(), nullable=True),
    Column("state", Text(), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("summary", Text(), nullable=True),
    CheckConstraint(
        "state IN ('active', 'ended')",
        name="state_valid",
    ),
    Index("ix_agent_runs_work_session_id_started_at", "work_session_id", "started_at"),
    Index("ix_agent_runs_agent_name", "agent_name"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0007_create_agent_runs_table``."""
