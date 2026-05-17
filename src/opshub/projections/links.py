"""``links`` read-model projection (Phase 8 step A2, ADR-0017).

Materialises cross-entity links derived from existing events
(``ProposalApplied`` / ``BriefingGenerated`` / ``ProposalRequested`` /
``SourceReferenced``) plus operator-asserted links (``LinkCreated`` /
``LinkDeleted``). The Phase 8 B2 ``LinksExtractor`` projector fills
in the dispatch logic; this PR provides the skeleton + schema only
so the registry list includes ``links`` from Phase 8 onwards.

Schema overview (1:1 with migration ``0016_create_links_table``):

* ``id`` — link ULID (PK).
* ``from_entity_type`` / ``from_entity_id`` — source side of the link.
* ``to_entity_type`` / ``to_entity_id`` — target side of the link.
* ``link_type`` — see ADR-0017 §決定 (b) for the MVP enum.
* ``created_at`` — business-time stamp the link was first observed.
* ``source_event_id`` — nullable ULID of the event that emitted or
  derived this link (``LinkCreated`` for manual links; the derived
  event id for auto-extracted ones). Audit / replay debug column.
* ``metadata`` — nullable JSON blob for link-type specific extras
  (e.g. recall score on ``referenced_in_briefing`` links).

Two indexes back bidirectional traversal:

* ``links_from_idx (from_entity_type, from_entity_id)`` — outgoing.
* ``links_to_idx (to_entity_type, to_entity_id)`` — incoming.

The natural key
``(from_entity_type, from_entity_id, to_entity_type, to_entity_id,
link_type)`` is enforced by ``links_natural_key_uq``. Phase 8 B2 will
apply rows via SQLite UPSERT keyed on the natural-key tuple so that
``projections rebuild`` is idempotent end-to-end (ADR-0017 §決定 (a)).

Phase 8 step A2 contract (this PR)
-----------------------------------

* The :class:`LinksProjector` reducer is intentionally a no-op. Phase
  8 step B2 replaces :meth:`LinksProjector.apply` with the actual
  event-to-link dispatch (``ProposalApplied`` / ``BriefingGenerated``
  / ``ProposalRequested`` / ``SourceReferenced`` / ``LinkCreated`` /
  ``LinkDeleted``).
* The skeleton exists in A2 so that :func:`opshub.projections.registry.all_projections`
  already lists ``links`` once migration 0016 lands. Without the
  registry entry, a freshly migrated DB would have the table sitting
  empty even after ``opshub projections rebuild`` — the inline
  projector (``opshub.cli._wiring._PersistingProjector``) and the
  rebuild driver both read :func:`all_projections`, so the registry
  is the SSOT.

Cold-start guard
----------------

Module-level imports are restricted to ``__future__`` / ``typing`` /
SQLAlchemy primitives + :data:`opshub.db.schema.metadata`. No LLM /
SDK / pydantic-heavy imports at top level — the projection module is
imported transitively by every ``opshub`` CLI invocation through the
registry, and cold-start cost matters (mirrors the M6 guard enforced
on ``opshub/cli/*.py``).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
)
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent

__all__ = ["LINK_TYPES_MVP", "LinksProjector", "links_table"]


# ADR-0017 §決定 (b): the 5 ``link_type`` values populated by automatic
# extraction in Phase 8 B2. Manual link CRUD via ``LinkCreated`` /
# ``LinkDeleted`` (Phase 8 B1 / D1) may pass arbitrary strings — the
# CLI warns when the value falls outside this enum but the projector
# writes the row through without further validation. Captured here as
# a ``frozenset`` so consumers (CLI warning helper / future graph
# rendering) can membership-test without recomputing the literal set.
LINK_TYPES_MVP: frozenset[str] = frozenset(
    {
        "applied_to",
        "referenced_in_briefing",
        "generated_from_briefing",
        "references",
        "manual",
    }
)


links_table: Table = Table(
    "links",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("from_entity_type", Text(), nullable=False),
    Column("from_entity_id", String(length=26), nullable=False),
    Column("to_entity_type", Text(), nullable=False),
    Column("to_entity_id", String(length=26), nullable=False),
    Column("link_type", Text(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("source_event_id", String(length=26), nullable=True),
    Column("metadata", JSON(), nullable=True),
    UniqueConstraint(
        "from_entity_type",
        "from_entity_id",
        "to_entity_type",
        "to_entity_id",
        "link_type",
        name="links_natural_key_uq",
    ),
    Index("links_from_idx", "from_entity_type", "from_entity_id"),
    Index("links_to_idx", "to_entity_type", "to_entity_id"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0016_create_links_table``.

The natural key on
``(from_entity_type, from_entity_id, to_entity_type, to_entity_id,
link_type)`` is enforced via ``links_natural_key_uq``. Phase 8 B2
will use this constraint as the conflict target for the SQLite
``INSERT ... ON CONFLICT ... DO UPDATE`` (UPSERT) so re-applying the
same derived event on ``projections rebuild`` collapses onto the
existing row instead of raising on the unique violation.

``metadata`` is sa.JSON so SQLAlchemy adapts arbitrary mappings via
the SQLite stdlib JSON codec; the projection layer treats the value
as opaque (mirrors the Phase 5 ``source_refs`` / Phase 6
``candidates`` treatment).
"""


class LinksProjector:
    """Phase 8 step A2 skeleton: ``apply`` is a no-op pending B2 logic.

    Registered in :func:`opshub.projections.registry.all_projections`
    from Phase 8 onwards so that the inline projector wiring and the
    rebuild driver both know about ``links``. The reducer body itself
    is a no-op until Phase 8 step B2 lands the dispatch table for the
    six link-emitting event types (``ProposalApplied`` /
    ``BriefingGenerated`` / ``ProposalRequested`` / ``SourceReferenced``
    / ``LinkCreated`` / ``LinkDeleted``).

    Splitting the skeleton (A2) from the extraction logic (B2) keeps
    each PR scoped to one concern: A2 pins the physical schema +
    registry presence, B2 pins the per-event row generation. Tests in
    A2 lock the no-op contract so a regression that accidentally
    writes rows from the skeleton is caught at review time.

    Each statement runs on the Connection passed in by the rebuild
    driver — the projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection`).
    """

    name = "links"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Phase 8 A2 no-op skeleton.

        Phase 8 step B2 replaces this body with the dispatch table
        per ADR-0017 §決定 (c). Until then the projector intentionally
        accepts every event and writes nothing, so adding it to the
        :func:`all_projections` registry in A2 does not change the
        observable read-model state for any existing event family.
        """
        # B2 will dispatch on ``isinstance(event, ...)`` here. The A2
        # skeleton is intentionally inert — see the module docstring
        # for the rationale.
        return None

    def reset(self, conn: Connection) -> None:
        """Empty the ``links`` table.

        Issued by the rebuild driver before replay so the projection
        reflects exactly the events currently in the store. The Phase
        8 A2 skeleton already wires this through because Phase 8 B2's
        idempotent-rebuild test depends on ``reset`` being functional
        from day one.
        """
        conn.execute(delete(links_table))
