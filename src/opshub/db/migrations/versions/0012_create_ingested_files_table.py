"""Create the ingested_files projection table.

Revision ID: 0012_create_ingested_files_table
Revises: 0011_create_connector_cursors_table
Create Date: 2026-05-17

The ``ingested_files`` table is the canonical read model for the
workspace file ingest path (Phase 3 step C2, ADR-0002). One row exists
per **content hash** (SHA-256 hex) ever ingested from
``workspace/inbox/*.md`` — the primary key is the content hash so the
:class:`opshub.services.file_ingest_service.FileIngestService` can do
an O(1) "have I already ingested this content?" check on each scan.

Columns:

* ``content_hash`` — SHA-256 hex digest (64 chars); primary key. The
  natural idempotency key for the inbox file ingest pipeline.
* ``file_path`` — the last-seen path that produced this content. May
  differ across renames / moves of the same content; we keep the most
  recent one to help operators debug "where did that come from?"
  questions. NOT a unique key — two paths with identical content
  collapse to a single row.
* ``inbox_item_id`` — ULID of the :class:`ItemEnqueued` event that
  this file produced on first ingest. Kept stable across rename /
  re-ingest so a future "find the inbox item for this file" lookup
  resolves to the first observation, not a fresh one.
* ``ingested_at`` — when this content was last ingested (refreshed on
  the upsert path so operators can see "this file was re-scanned at
  T+1" even when the body did not change).

Indexes:

* ``ix_ingested_files_file_path`` — non-unique. Enables debugging
  queries that ask "which content hashes have ever lived at this
  path?" without scanning the whole table.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_create_ingested_files_table"
down_revision: str | Sequence[str] | None = "0011_create_connector_cursors_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``ingested_files`` projection table and its file_path index."""
    op.create_table(
        "ingested_files",
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("inbox_item_id", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("content_hash", name=op.f("pk_ingested_files")),
    )
    op.create_index(
        op.f("ix_ingested_files_file_path"),
        "ingested_files",
        ["file_path"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``ingested_files`` table (index drops with it)."""
    op.drop_index(op.f("ix_ingested_files_file_path"), table_name="ingested_files")
    op.drop_table("ingested_files")
