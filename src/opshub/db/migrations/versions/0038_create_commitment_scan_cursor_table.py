"""Create the commitment_scan_cursor projection table.

Revision ID: 0038_create_commitment_scan_cursor_table
Revises: 0037_create_commitments_table
Create Date: 2026-06-14

Phase 25-C ([epic #566](https://github.com/ozzy-labs/opshub/issues/566),
[ADR-0042](../../../docs/adr/0042-commitment-ledger.md)) needs an
incremental scan checkpoint so ``opshub commitment scan`` only re-reads
sources observed since the last scan (the LLM cost is paid once per
source). opshub has no non-connector checkpoint mechanism — the only
precedent is ``connector_cursors`` (per-connector sync progress) — so
this migration ports that pattern to a singleton commitment-scan cursor.

There is exactly **one** row (keyed on the literal ``"commitment_scan"``
singleton), because the scan sweeps every connector's ``sources`` as a
single stream rather than per-connector. ``cursor_value`` is the highest
source ``id`` (a monotonic ULID) the last completed scan extracted from;
the next scan resumes from sources with a greater ``id``.

Columns (mirroring the ``connector_cursors`` shape):

* ``scan_key`` — the singleton primary key (always ``"commitment_scan"``).
* ``cursor_value`` — the source-id watermark the last completed scan
  reached (``NULL`` before the first scan = "scan everything").
* ``updated_at`` — refreshed when the watermark advances on completion.
* ``last_scanned_at`` — start-of-scan wall clock (matches the
  ``connector_cursors.last_synced_at`` start-time semantic: operators care
  more about "when did the last scan attempt happen" than "when did it
  finish").

The cursor is event-sourced (``CommitmentScanStarted`` upserts the
resume-from value + start anchor; ``CommitmentScanCompleted`` advances the
watermark; ``CommitmentScanFailed`` is a no-op so the watermark stays put
and the next scan re-attempts), so ``projections rebuild`` reconstructs
it deterministically from the event log.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038_create_commitment_scan_cursor_table"
down_revision: str | Sequence[str] | None = "0037_create_commitments_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the singleton ``commitment_scan_cursor`` table."""
    op.create_table(
        "commitment_scan_cursor",
        sa.Column("scan_key", sa.Text(), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scan_key", name=op.f("pk_commitment_scan_cursor")),
    )


def downgrade() -> None:
    """Drop the ``commitment_scan_cursor`` table."""
    op.drop_table("commitment_scan_cursor")
