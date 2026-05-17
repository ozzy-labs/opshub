"""Create the embeddings_vec virtual tables (Phase 4 step A1).

Revision ID: 0013_create_embeddings_vec_table
Revises: 0012_create_ingested_files_table
Create Date: 2026-05-17

Phase 4 (ADR-0012 §5 / phase-4-plan §1 確定事項 #5 + §4 Open Q
resolution) splits the embedding store into two physical objects:

* A metadata-only :class:`embeddings` table (``entity_type``,
  ``entity_id``, ``model_id``, ``model_version``, ``dim``,
  ``created_at``) — what we already provision in migration 0002 minus
  the ``vector BLOB`` column.
* A family of ``vec0`` virtual tables — one per embedding backend —
  that hold the actual float vectors and answer ``MATCH`` queries.
  ``rowid`` is the JOIN key against ``embeddings``.

Per phase-4-plan §1 #5 + §4 Open Q解消 the backend-per-table layout
was chosen over a single shared table because each backend ships a
different fixed dimension (``local=BAAI/bge-m3`` 1024-dim,
``openai=text-embedding-3-small`` 1536-dim,
``voyage=voyage-3`` 1024-dim) and sqlite-vec's ``vec0`` virtual table
pins the dimension at creation time. Phase 4 MVP writes / reads only
the currently active backend; parallel multi-backend retention is
deferred to Phase 4.x.

Operational requirement: ``CREATE VIRTUAL TABLE ... USING vec0`` only
succeeds when the sqlite-vec extension is loaded on the connection.
:func:`opshub.db.engine.create_engine_for_sqlite` loads it via a
``connect`` listener when the ``[vector]`` extras are installed. Without
the extras, ``alembic upgrade`` will fail on this revision with
``no such module: vec0`` — install ``opshub[vector]`` first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_create_embeddings_vec_table"
down_revision: str | Sequence[str] | None = "0012_create_ingested_files_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Backend-specific ``vec0`` virtual tables. Dimensions are pinned per
# phase-4-plan §4 Open Q解消 and ADR-0012 §5. Adding a backend means
# appending a new tuple here AND issuing a follow-up migration; SQLite
# does not let you ALTER a ``vec0`` virtual table to widen its
# dimension after creation.
_VEC_TABLES: tuple[tuple[str, int], ...] = (
    ("embeddings_vec_local", 1024),
    ("embeddings_vec_openai", 1536),
    ("embeddings_vec_voyage", 1024),
)


def upgrade() -> None:
    """Drop ``embeddings.vector`` and create the per-backend vec0 tables.

    The ``vector BLOB`` column added by migration 0002 becomes dead
    weight in Phase 4: vectors now live in the ``vec0`` virtual tables
    keyed by ``rowid``. We drop it via :func:`op.batch_alter_table` so
    Alembic uses SQLite's copy-and-recreate emulation (the env.py
    already configures ``render_as_batch=True``; the explicit batch
    block here is belt-and-braces for offline ``--sql`` review).
    """
    with op.batch_alter_table("embeddings") as batch_op:
        batch_op.drop_column("vector")

    # ``CREATE VIRTUAL TABLE`` is not part of Alembic's high-level
    # operation set; emit raw SQL. Quoting is unnecessary — the names
    # are static identifiers and the dimensions are integer literals.
    for table_name, dim in _VEC_TABLES:
        op.execute(f"CREATE VIRTUAL TABLE {table_name} USING vec0(embedding float[{dim}])")


def downgrade() -> None:
    """Drop the vec0 tables and restore the Phase 1 ``vector`` column.

    Downgrade is intentionally non-destructive of the ``embeddings``
    rows themselves: any pre-existing rows will have an empty BLOB
    placeholder in the restored column (``DEFAULT (X'')``), since
    Phase 4 never wrote real bytes back into the table. Operators who
    need real vectors after a rollback must re-run the embedder.
    """
    for table_name, _dim in _VEC_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table_name}")

    with op.batch_alter_table("embeddings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "vector",
                sa.LargeBinary(),
                nullable=False,
                server_default=sa.text("X''"),
            )
        )
