"""Create the person_identities projection table.

Revision ID: 0036_create_person_identities_table
Revises: 0035_create_persons_table
Create Date: 2026-06-14

Phase 25-B ([epic #566](https://github.com/ozzy-labs/opshub/issues/566),
[ADR-0043](../../../docs/adr/0043-cross-source-identity-resolution.md))
materialises the connector-native identities that resolve to a person
(migration 0035). One row exists per ``(connector, handle)`` join key
the resolution service discovers in the ``sources`` author columns
(Phase 25-A) and binds to a person.

Columns:

* ``connector`` / ``handle`` — the natural key. ``connector`` is the
  producing connector (``slack`` / ``github`` / ``google_mail`` / ...);
  ``handle`` is the normalised ``author_handle`` the ``sources``
  projection stored (Slack ``U...`` / lower-cased email / GitHub
  login). The pair is namespaced by connector so a ``U...``-shaped
  Slack handle never collides with a like-shaped handle from another
  connector (the same self-describing rule migration 0034 pins for
  ``sources.author_connector``).
* ``person_id`` — FK to ``persons.id``. ``IdentityMerged`` re-parents
  this column; ``IdentitySplit`` repoints it to a fresh person.
* ``display`` — optional display name observed alongside the handle
  (recognition cue, never a join key).
* ``confidence`` — how the link was decided (``exact`` for an
  auto-merged exact handle/email match, ``manual`` for an operator
  ``opshub person merge`` / ``split``).
* ``linked_at`` — business-time the identity was first bound.

The ``UNIQUE(connector, handle)`` constraint enforces that one
connector-native identity maps to exactly one person — the resolver's
UPSERT collides on it so re-running ``opshub person`` (or replaying the
event log) is idempotent. ``ix_person_identities_person_id`` backs the
"all identities of this person" lookup the graph + commitment ledger
run.

The FK is declared ``ON DELETE CASCADE`` so a tombstoned (merged-away)
person cannot leave orphan identities; the projection re-parents
identities *before* deleting the merged person row, so the cascade is
defence-in-depth rather than the primary path.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036_create_person_identities_table"
down_revision: str | Sequence[str] | None = "0035_create_persons_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``person_identities`` + UNIQUE(connector, handle) + person_id index."""
    op.create_table(
        "person_identities",
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("person_id", sa.String(length=26), nullable=False),
        sa.Column("display", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "connector",
            "handle",
            name=op.f("pk_person_identities"),
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name=op.f("fk_person_identities_person_id_persons"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_person_identities_person_id"),
        "person_identities",
        ["person_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``person_identities`` table (index + FK drop with it)."""
    op.drop_index(
        op.f("ix_person_identities_person_id"),
        table_name="person_identities",
    )
    op.drop_table("person_identities")
