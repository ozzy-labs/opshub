"""Create the links projection table.

Revision ID: 0016_create_links_table
Revises: 0015_create_proposals_table
Create Date: 2026-05-17

The ``links`` table is the canonical read model for the Phase 8
Knowledge graph layer (ADR-0017 / phase-8-plan §2.1 A2). One row
exists per directional link between two entities; the natural key
``(from_entity_type, from_entity_id, to_entity_type, to_entity_id,
link_type)`` (ADR-0017 §決定 (a)) is enforced via a ``UNIQUE``
constraint so Phase 8 B2's UPSERT-on-derive semantics make
``projections rebuild`` idempotent.

Phase 8 step A2 (this migration) creates the schema only — the row
extraction logic lands in step B2 (`LinksExtractor` inside
:class:`opshub.projections.links.LinksProjector`). Until B2 ships the
table is intentionally empty after every rebuild, because the A2
projector skeleton is a no-op (see
:mod:`opshub.projections.links`).

Columns:

* ``id`` — link ULID; primary key. Phase 8 D1's ``opshub link add``
  CLI mints this via :func:`opshub.core.ids.new_ulid` for manual
  links; auto-extracted links derive a ULID from the source event id
  (Phase 8 B2 implementation detail).
* ``from_entity_type`` / ``from_entity_id`` — source side of the
  link. ``from_entity_type`` is a free-form string holding values
  such as ``"task" | "decision" | "inbox_item" | "source" |
  "briefing" | "proposal"`` (the Phase 1-6 aggregates; not
  constrained at the DB layer because future entity types must be
  addable without a migration).
* ``to_entity_type`` / ``to_entity_id`` — target side of the link.
* ``link_type`` — see ADR-0017 §決定 (b) for the Phase 8 MVP enum
  (``applied_to`` / ``referenced_in_briefing`` /
  ``generated_from_briefing`` / ``references`` / ``manual``). The
  column is unconstrained at the DB layer; manual link CRUD (Phase 8
  D1) may pass arbitrary strings — the CLI warns when the value
  falls outside the enum but the projector accepts the row.
* ``created_at`` — business-time stamp the link was first observed
  (the source event's ``occurred_at`` for auto-extracted links, the
  ``LinkCreated`` event's ``occurred_at`` for manual links).
* ``source_event_id`` — ULID of the event that emitted or derived
  this link. Nullable because some manual links emitted before the
  ``LinkCreated`` event family was introduced (Phase 8 B1) would not
  have one — Phase 8 onwards always populates it.
* ``metadata`` — nullable JSON blob for link-type specific extras.
  Phase 8.x may store the recall score on
  ``referenced_in_briefing`` rows here, for instance.

Two indexes back bidirectional traversal (ADR-0017 §決定 (a)):

* ``links_from_idx (from_entity_type, from_entity_id)`` — outgoing
  edge lookup (used by ``LinkService.related(direction="outgoing")``
  and ``trace``).
* ``links_to_idx (to_entity_type, to_entity_id)`` — incoming edge
  lookup (used by ``related(direction="incoming")`` and ``expand``).

The ``links_natural_key_uq`` UNIQUE constraint is the idempotency
anchor for Phase 8 B2's UPSERT: re-applying the same derived event
on rebuild collapses onto the existing row rather than raising on the
unique violation. Phase 8 B2 will target this constraint with SQLite's
``INSERT ... ON CONFLICT (from_entity_type, ..., link_type) DO UPDATE
SET ...``.

Migrations are intentionally self-contained: no imports from
``opshub.domain`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_create_links_table"
down_revision: str | Sequence[str] | None = "0015_create_proposals_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``links`` projection table + 2 traversal indexes."""
    op.create_table(
        "links",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("from_entity_type", sa.Text(), nullable=False),
        sa.Column("from_entity_id", sa.String(length=26), nullable=False),
        sa.Column("to_entity_type", sa.Text(), nullable=False),
        sa.Column("to_entity_id", sa.String(length=26), nullable=False),
        sa.Column("link_type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_event_id", sa.String(length=26), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_links")),
        sa.UniqueConstraint(
            "from_entity_type",
            "from_entity_id",
            "to_entity_type",
            "to_entity_id",
            "link_type",
            name="links_natural_key_uq",
        ),
    )
    op.create_index(
        "links_from_idx",
        "links",
        ["from_entity_type", "from_entity_id"],
        unique=False,
    )
    op.create_index(
        "links_to_idx",
        "links",
        ["to_entity_type", "to_entity_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``links`` table (cascades the two traversal indexes)."""
    op.drop_index("links_to_idx", table_name="links")
    op.drop_index("links_from_idx", table_name="links")
    op.drop_table("links")
