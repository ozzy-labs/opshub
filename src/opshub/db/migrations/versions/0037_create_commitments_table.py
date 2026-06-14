"""Create the commitments projection table.

Revision ID: 0037_create_commitments_table
Revises: 0036_create_person_identities_table
Create Date: 2026-06-14

Phase 25-C ([epic #566](https://github.com/ozzy-labs/opshub/issues/566),
[ADR-0042](../../../docs/adr/0042-commitment-ledger.md)) is the旗艦 of
the 秘書化 v1 epic: a two-way commitment ledger mined from the data
opshub already ingests. This table is the canonical read model for the
commitment aggregate — one row per LLM-extracted commitment, keyed by the
commitment's own ULID.

Columns:

* ``id`` — the commitment ULID minted by the scan service
  (``CommitmentExtracted.aggregate_id``).
* ``source_id`` / ``source_type`` — the ``sources`` row the commitment was
  mined from. ``UNIQUE(source_id, source_type)`` enforces one commitment
  per source so a re-scan UPSERTs in place rather than duplicating
  (mirrors the ``inbox_items`` ``source_ref`` invariant, ADR-0010).
* ``direction`` — ``"i_owe"`` (operator-authored promise) or
  ``"owed_to_me"`` (request the operator received). Decided from the
  Phase 25-A operator-self-id signal + the LLM body reading.
* ``counterparty`` — the other party as a ``person:<id>`` graph ref
  (Phase 25-B) when resolvable, else ``NULL``.
* ``due`` — free-form ISO-ish due date the LLM extracted, or ``NULL``.
* ``text`` — the one-line commitment summary.
* ``confidence`` — ``"high"`` / ``"medium"`` / ``"low"`` (the LLM's
  self-report), so the operator can triage low-confidence rows first.
* ``state`` — ``"open"`` / ``"resolved"`` / ``"dismissed"``. Defaults to
  ``"open"`` on extraction; the operator transitions it via
  ``resolve`` / ``dismiss`` / ``reopen``.
* ``model_id`` / ``tokens_in`` / ``tokens_out`` — the cost trace.
* ``extracted_at`` — business-time the commitment was extracted
  (``CommitmentExtracted.occurred_at``).
* ``updated_at`` — refreshed on every state transition.

Indexes back the two list axes the ledger surfaces: ``direction`` (i-owe
vs owed-to-me) and ``state`` (open ledger filtering). There is no FK to
``sources`` — replay order is not guaranteed to land the source row
before the commitment row, and the ``source_ref`` is a logical join, not
a referential one (matching the ``inbox_items`` source_ref convention).

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037_create_commitments_table"
down_revision: str | Sequence[str] | None = "0036_create_person_identities_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``commitments`` projection table + indexes."""
    op.create_table(
        "commitments",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("source_id", sa.String(length=26), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("counterparty", sa.Text(), nullable=True),
        sa.Column("due", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=False, server_default="medium"),
        sa.Column("state", sa.Text(), nullable=False, server_default="open"),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commitments")),
        sa.UniqueConstraint(
            "source_id",
            "source_type",
            name=op.f("uq_commitments_source_id_source_type"),
        ),
    )
    op.create_index(op.f("ix_commitments_direction"), "commitments", ["direction"], unique=False)
    op.create_index(op.f("ix_commitments_state"), "commitments", ["state"], unique=False)


def downgrade() -> None:
    """Drop the ``commitments`` table (indexes drop with it)."""
    op.drop_index(op.f("ix_commitments_state"), table_name="commitments")
    op.drop_index(op.f("ix_commitments_direction"), table_name="commitments")
    op.drop_table("commitments")
