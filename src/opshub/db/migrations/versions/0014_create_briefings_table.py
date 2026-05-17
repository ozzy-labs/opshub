"""Create the briefings projection table.

Revision ID: 0014_create_briefings_table
Revises: 0013_create_embeddings_vec_table
Create Date: 2026-05-17

The ``briefings`` table is the canonical read model for the briefing
aggregate (Phase 5 step B2, ADR-0002 / phase-5-plan §2.2 B2). One row
exists per successfully generated briefing
(:class:`opshub.domain.events.BriefingGenerated`); the
``BriefingRequested`` / ``BriefingFailed`` events are deliberately
events-table-only (mirroring the Phase 2 lock-bracket pattern) so a
failure or pending request never materialises a row whose ``markdown``
column would be empty.

Columns:

* ``id`` — briefing ULID (the event's ``aggregate_id``); primary key.
* ``topic`` — free-form briefing topic ("what was the user asking
  about"). NOT NULL.
* ``scope`` — stringified scope value. ``"all"`` for Phase 5 MVP;
  future ``"task:<id>"`` / ``"project:<id>"`` narrow scopes (Phase
  5.x) collapse onto the same column type.
* ``markdown`` — rendered briefing body.
* ``source_refs`` — JSON-serialised list of ``(entity_type,
  entity_id)`` tuples the BriefingService fed to the LLM prompt.
  ``sa.JSON`` lets SQLAlchemy adapt the value via the dialect's JSON
  codec; SQLite stores it as TEXT.
* ``model_id`` — LLM backend id at generation time (e.g.
  ``"claude-haiku-4-5-20251001"``).
* ``model_version`` — optional version string distinct from
  ``model_id``. Nullable per :class:`opshub.llm.client.LLMResponse`
  contract: some backends (notably the future local-LLM backend)
  may not surface a version separate from ``model_id``.
* ``tokens_in`` / ``tokens_out`` — non-negative cost trace surfaced
  by the LLM client.
* ``generated_at`` — business-time stamp from the
  ``BriefingGenerated`` event.

The PK on ``id`` is the idempotency anchor for the upsert in
:class:`opshub.projections.briefings.BriefingsProjection`: replaying
the same event on rebuild collapses onto the existing row instead of
raising on a PK collision.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_create_briefings_table"
down_revision: str | Sequence[str] | None = "0013_create_embeddings_vec_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``briefings`` projection table."""
    op.create_table(
        "briefings",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefings")),
    )


def downgrade() -> None:
    """Drop the ``briefings`` table."""
    op.drop_table("briefings")
