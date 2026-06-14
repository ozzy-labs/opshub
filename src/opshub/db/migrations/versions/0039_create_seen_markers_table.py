"""Create the seen_markers projection table.

Revision ID: 0039_create_seen_markers_table
Revises: 0038_create_commitment_scan_cursor_table
Create Date: 2026-06-14

Phase 25-E ([epic #566](https://github.com/ozzy-labs/opshub/issues/566))
needs a durable "when did the operator last catch up?" anchor so
``opshub catchup --since-last-seen`` only re-surfaces the diff that
accrued after the previous run (new sources / overdue commitments / open
Slack demand). opshub's only non-connector checkpoint precedents are
``connector_cursors`` (Phase 3) and ``commitment_scan_cursor`` (Phase
25-C); this migration ports the same singleton-checkpoint pattern to the
catchup seen marker.

There is exactly **one** row (keyed on the literal ``"catchup"``
singleton ``marker_key``), because catchup sweeps every connector's
``sources`` + the commitment ledger + the Slack demand digest as a single
stream rather than per-connector.

Columns:

* ``marker_key`` — the singleton primary key (always ``"catchup"``).
* ``seen_at`` — the business-time watermark the last catchup treats as
  "the operator has seen everything up to here". A subsequent
  ``catchup --since-last-seen`` filters the diff on it. ``NULL`` is never
  stored — the first :class:`SeenMarkerAdvanced` seeds it.
* ``updated_at`` — refreshed whenever the marker advances.

The marker is event-sourced (:class:`SeenMarkerAdvanced` upserts the
singleton row with the new ``seen_at``), so ``projections rebuild``
reconstructs it deterministically from the event log (last writer wins).

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039_create_seen_markers_table"
down_revision: str | Sequence[str] | None = "0038_create_commitment_scan_cursor_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the singleton ``seen_markers`` table."""
    op.create_table(
        "seen_markers",
        sa.Column("marker_key", sa.Text(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("marker_key", name=op.f("pk_seen_markers")),
    )


def downgrade() -> None:
    """Drop the ``seen_markers`` table."""
    op.drop_table("seen_markers")
