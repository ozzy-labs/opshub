"""Add the ``fingerprint`` column to the ``sources`` projection table.

Revision ID: 0017_add_fingerprint_to_sources
Revises: 0016_create_links_table
Create Date: 2026-05-23

This migration is the Phase 9 step A2 foundation for the
``box_drive`` connector's diff-detection path (ADR-0019 §決定 (d)).
The :class:`~opshub.connectors.box_drive.scanner.BoxDriveScanner`
needs to suppress :class:`SourceObserved` event noise across the
100k+ files that typically sit under a Box Drive mount, so the scanner
loads ``(external_id, fingerprint)`` for prior observations from this
column at the start of each sync and only emits an event when the
file's current ``f"{size}:{mtime_ns}"`` fingerprint disagrees with the
stored value.

The column is intentionally **nullable**:

* The four pre-existing connectors (``github`` / ``slack`` / ``ms365``
  / ``box``) never populate a fingerprint — they rely on remote sync
  cursors, not local stat() metadata — so their rows land with
  ``fingerprint = NULL``.
* ``SourceObserved.fingerprint`` is itself ``str | None = None`` per
  ADR-0019 §決定 (d) (backward-compatible field addition, ADR-0002
  §4), so historic events replayed by ``projections rebuild`` produce
  the same ``NULL`` write through the projector.

SQLite's ``ALTER TABLE sources ADD COLUMN fingerprint TEXT`` is a
single-statement operation (https://www.sqlite.org/lang_altertable.html),
so Alembic's batch mode is not required and the migration sticks to
the plain ``op.add_column`` / ``op.drop_column`` shape used by every
prior table-shape change in this tree.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_add_fingerprint_to_sources"
down_revision: str | Sequence[str] | None = "0016_create_links_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``sources.fingerprint TEXT NULL``."""
    op.add_column(
        "sources",
        sa.Column("fingerprint", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop ``sources.fingerprint``."""
    op.drop_column("sources", "fingerprint")
