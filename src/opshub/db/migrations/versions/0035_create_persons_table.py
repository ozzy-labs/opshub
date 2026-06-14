"""Create the persons projection table.

Revision ID: 0035_create_persons_table
Revises: 0034_add_author_to_sources
Create Date: 2026-06-14

Phase 25-B ([epic #566](https://github.com/ozzy-labs/opshub/issues/566),
[ADR-0043](../../../docs/adr/0043-cross-source-identity-resolution.md))
introduces the **person-axis** the 秘書化 v1 commitment ledger (25-C)
builds "who owes whom" on. A person is one human who shows up across
connectors under different author handles (Slack ``U...`` / email /
GitHub ``login``); this table is the canonical read model for the
person aggregate, keyed by the person's own ULID.

Columns:

* ``id`` — the person ULID minted by the resolution service. Stable
  across re-resolution; the ``person:<id>`` graph entity ref
  (ADR-0017 §改訂) points at it.
* ``display_name`` — recognition cue (the connector's display name, or
  the handle when none is exposed). Never a join key — identity
  matching happens on ``person_identities.handle`` (migration 0036).
* ``is_operator`` — ``1`` for the single person representing the
  operator themselves (ADR-0043), ``0`` otherwise. Stored as an
  integer because SQLite has no native boolean.
* ``created_at`` — business-time the person was first identified
  (``PersonIdentified.occurred_at``).
* ``updated_at`` — refreshed when an :class:`IdentityMerged` re-parents
  identities onto this surviving person.

An index on ``is_operator`` keeps the "find the operator person" lookup
the commitment ledger runs cheap. There is no FK to ``person_identities``
— the identities table carries the ``person_id`` FK in the other
direction (migration 0036), matching the connector_cursors / sources
one-table-per-aggregate convention.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035_create_persons_table"
down_revision: str | Sequence[str] | None = "0034_add_author_to_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``persons`` projection table + ``is_operator`` index."""
    op.create_table(
        "persons",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_operator", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_persons")),
    )
    op.create_index(
        op.f("ix_persons_is_operator"),
        "persons",
        ["is_operator"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``persons`` table (index drops with it)."""
    op.drop_index(op.f("ix_persons_is_operator"), table_name="persons")
    op.drop_table("persons")
