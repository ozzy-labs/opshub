"""Create the sources projection table.

Revision ID: 0010_create_sources_table
Revises: 0009_create_handoffs_table
Create Date: 2026-05-17

The ``sources`` table is the canonical read model for the source
aggregate (Phase 3, ADR-0002). One row exists per external item the
connectors have observed — a GitHub issue, a pull request, a Slack
notification, etc. Re-observing the same external item is an upsert
keyed on ``(connector_name, external_id)`` rather than appending a new
row (phase-3-plan §3 機能 §3).

Columns:

* ``id`` — the source ULID we mint locally; stable across re-observations.
* ``connector_name`` — e.g. ``"github"``, ``"slack"``. The connector
  layer interprets it; the DB only stores it as opaque text.
* ``external_id`` — the connector's native ID for the item. Format is
  the connector's choice (``"123"`` for a GitHub issue, ``"owner/repo#42"``
  for a fully-qualified ref, a Slack ``thread_ts``, etc.).
* ``source_type`` — e.g. ``"issue"``, ``"pull_request"``, ``"notification"``;
  again opaque text at this layer.
* ``title`` — short display title.
* ``url`` — canonical link back to the external item; nullable because
  some sources (e.g. raw notifications) may not have a stable URL.
* ``summary`` — optional human / LLM-generated digest.
* ``observed_at`` — when the connector first saw the item (business time).
* ``updated_at`` — when the connector last refreshed the row.

Constraints / indexes:

* ``UNIQUE(connector_name, external_id)`` — enables the upsert semantics
  required by ADR-0002 / phase-3-plan §3 機能 §3. A second observation
  of the same external item must collide on this constraint and route
  the connector through the update path.
* ``ix_sources_connector_name`` — connector-scoped sync-time scan.
* ``ix_sources_updated_at`` — recent-activity queries (markdown
  rendering, CLI list views).

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_create_sources_table"
down_revision: str | Sequence[str] | None = "0009_create_handoffs_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``sources`` projection table, UNIQUE constraint and indexes."""
    op.create_table(
        "sources",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("connector_name", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint(
            "connector_name",
            "external_id",
            name=op.f("uq_sources_connector_name_external_id"),
        ),
    )
    op.create_index(
        op.f("ix_sources_connector_name"),
        "sources",
        ["connector_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sources_updated_at"),
        "sources",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``sources`` table (UNIQUE + indexes drop with it)."""
    op.drop_index(op.f("ix_sources_updated_at"), table_name="sources")
    op.drop_index(op.f("ix_sources_connector_name"), table_name="sources")
    op.drop_table("sources")
