"""Add ``body`` + provenance columns to the ``sources`` projection table.

Revision ID: 0018_add_body_provenance_to_sources
Revises: 0017_add_fingerprint_to_sources
Create Date: 2026-05-30

Phase 10 step A2 foundation for **Full Local Content Retention**
(ADR-0020, which supersedes ADR-0005 External Content Minimization).
Connectors now retain the full body of an observed item rather than
only a ≤200-char summary, so the ``sources`` projection grows three
columns:

* ``body`` (``TEXT``) — the full retained content of the observed item.
* ``provenance_origin`` (``TEXT``) — ``"external"`` (fetched from a
  SaaS / local FS by a connector) or ``"internal"`` (operator-authored
  workspace ingest / opshub-generated). ADR-0020 §(e).
* ``provenance_trust`` (``TEXT``) — ``"trusted"`` / ``"untrusted"``.
  External connector bodies are tagged ``untrusted`` so an agent / LLM
  treats them as reference material, never instructions (content
  poisoning / indirect prompt-injection mitigation, ADR-0020 §(e) +
  ADR-0015 §決定 (f)).

All three columns are intentionally **nullable**:

* Phase 3-9 rows written before this migration have no body /
  provenance — they land with ``NULL`` and behave exactly as before
  (ADR-0020 §(d) backward-compat). The four Web-API connectors will
  start populating ``body`` / provenance on their next sync, but the
  ``box_drive`` connector leaves ``body = NULL`` permanently (reading
  file contents is forbidden by ADR-0019 §不変条件 (b)).
* ``SourceObserved.body`` / ``provenance_*`` are themselves
  ``... | None = None`` (backward-compatible field addition, ADR-0002
  §4), so historic events replayed by ``projections rebuild`` produce
  the same ``NULL`` writes through the projector.

SQLite's ``ALTER TABLE sources ADD COLUMN ...`` is a single-statement
operation per column, so Alembic batch mode is not required — the
migration uses plain ``op.add_column`` / ``op.drop_column`` like every
prior table-shape change in this tree (cf. ``0017``).

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_add_body_provenance_to_sources"
down_revision: str | Sequence[str] | None = "0017_add_fingerprint_to_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``sources.body`` + ``provenance_origin`` + ``provenance_trust`` (all NULL)."""
    op.add_column("sources", sa.Column("body", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("provenance_origin", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("provenance_trust", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the three Phase 10 columns (other ``sources`` columns untouched)."""
    op.drop_column("sources", "provenance_trust")
    op.drop_column("sources", "provenance_origin")
    op.drop_column("sources", "body")
