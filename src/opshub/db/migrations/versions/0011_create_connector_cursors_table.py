"""Create the connector_cursors projection table.

Revision ID: 0011_create_connector_cursors_table
Revises: 0010_create_sources_table
Create Date: 2026-05-17

The ``connector_cursors`` table tracks per-connector sync progress
(Phase 3, ADR-0002). Exactly one row per connector: the row's
``cursor_value`` is an opaque string the connector understands (e.g. an
ISO timestamp for GitHub's ``since=...`` parameter, a Slack
``latest=...`` thread ts, etc.) — the DB does not impose a format and
treats it as text.

Columns:

* ``connector_name`` — primary key; ``"github"``, ``"slack"``, etc.
  UNIQUE is implicit via PK so re-running a sync overwrites the
  previous cursor.
* ``cursor_value`` — opaque string; nullable so the very first sync
  for a connector can write a row before any cursor has been issued.
* ``updated_at`` — when this row was last written.
* ``last_synced_at`` — when the connector last finished a sync pass
  (i.e. the business-time anchor for "how stale is this connector").

Indexes: none. Lookup is always by PK (``WHERE connector_name = ?``)
and there is at most one row per connector, so a secondary index would
be dead weight.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_create_connector_cursors_table"
down_revision: str | Sequence[str] | None = "0010_create_sources_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``connector_cursors`` projection table."""
    op.create_table(
        "connector_cursors",
        sa.Column("connector_name", sa.Text(), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("connector_name", name=op.f("pk_connector_cursors")),
    )


def downgrade() -> None:
    """Drop the ``connector_cursors`` table."""
    op.drop_table("connector_cursors")
