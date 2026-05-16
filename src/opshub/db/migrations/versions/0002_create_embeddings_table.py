"""Create the embeddings table (Phase 4 placeholder).

Revision ID: 0002_create_embeddings_table
Revises: 0001_create_events_table
Create Date: 2026-05-17

ADR-0012 defines the embeddings store as a thin physical table keyed by
``(entity_type, entity_id, model_id, model_version)``. The table is
empty until Phase 4 wires in embedders + sqlite-vec; we provision it
now so the schema is stable and ``alembic upgrade head`` reaches a
predictable state regardless of phase boundaries.

Invariant: at most one row per ``(entity_type, entity_id, model_id,
model_version)`` tuple — enforced by a UNIQUE constraint. Re-embedding
with a new model version produces a separate row; the application
layer (Phase 4) decides when to garbage-collect superseded versions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_create_embeddings_table"
down_revision: str | Sequence[str] | None = "0001_create_events_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``embeddings`` table, its UNIQUE constraint and index."""
    op.create_table(
        "embeddings",
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "model_id",
            "model_version",
            name=op.f("uq_embeddings_entity_type_entity_id_model_id_model_version"),
        ),
    )
    op.create_index(
        op.f("ix_embeddings_entity_type_entity_id"),
        "embeddings",
        ["entity_type", "entity_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``embeddings`` table (indexes + UNIQUE drop with it)."""
    op.drop_index(
        op.f("ix_embeddings_entity_type_entity_id"),
        table_name="embeddings",
    )
    op.drop_table("embeddings")
