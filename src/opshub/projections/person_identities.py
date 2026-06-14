"""``person_identities`` read-model projection (Phase 25-B, ADR-0043).

The ``person_identities`` table is the canonical read model for the
connector-native identities that resolve to a person (the ``persons``
table, :mod:`opshub.projections.persons`). One row exists per
``(connector, handle)`` join key the resolution service
(:mod:`opshub.services.persons`) discovers in the ``sources`` author
columns (Phase 25-A) and binds to a person.

This projection owns **one** of the two person-axis tables and reduces
**one** event family:

* :class:`IdentityLinked` → ``INSERT ... ON CONFLICT(connector, handle)
  DO UPDATE`` that binds a connector identity to a person. The UPSERT
  collides on the natural key so re-running the resolver (or replaying
  the event log) is idempotent. ``person_id`` is refreshed on conflict
  so an :class:`IdentityLinked` re-emitted under a survivor person (the
  resolver records a fresh link, not a merge, when an exact match is
  found) re-parents the identity deterministically.

The cross-table merge / split mutations (:class:`IdentityMerged` /
:class:`IdentitySplit`) re-parent / repoint rows in *this* table, but
they are applied by :class:`~opshub.projections.persons.PersonsProjection`
so each event's two-table mutation stays atomic inside a single
projection ``apply`` (avoiding any inter-projection ordering hazard on
the ``person_id`` FK). This projection therefore deliberately ignores
those events — it only INSERTs the initial binding.

Column shape mirrors migration ``0036_create_person_identities_table``
(1:1). The :data:`person_identities_table` :class:`~sqlalchemy.Table`
is registered on the shared :data:`opshub.db.schema.metadata` at import
time so Alembic autogenerate sees it symmetrically with ``persons``.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    delete,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import DomainEvent, IdentityLinked

__all__ = ["PersonIdentitiesProjection", "person_identities_table"]


person_identities_table: Table = Table(
    "person_identities",
    metadata,
    Column("connector", Text(), nullable=False, primary_key=True),
    Column("handle", Text(), nullable=False, primary_key=True),
    Column(
        "person_id",
        String(length=26),
        ForeignKey("persons.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("display", Text(), nullable=True),
    Column("confidence", Text(), nullable=False),
    Column("linked_at", DateTime(timezone=True), nullable=False),
    Index("ix_person_identities_person_id", "person_id"),
)
"""SQLAlchemy ``Table`` mirroring ``0036_create_person_identities_table``."""


class PersonIdentitiesProjection:
    """Reducer mapping :class:`IdentityLinked` to ``person_identities`` rows.

    The reducer is a pure dispatch on event type: a single UPSERT per
    :class:`IdentityLinked`, keyed on the ``(connector, handle)`` natural
    key. Each statement runs on the Connection passed in by the rebuild
    driver (or the service-layer UoW) — the projection never opens its
    own transaction (see :class:`~opshub.projections.base.Projection`).

    :class:`IdentityMerged` / :class:`IdentitySplit` re-parent / repoint
    rows in this table but are owned by
    :class:`~opshub.projections.persons.PersonsProjection` (so the
    two-table mutation stays atomic); this reducer ignores them along
    with every other unrelated event type.
    """

    name = "person_identities"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the ``person_identities`` natural-key row."""
        if isinstance(event, IdentityLinked):
            self._apply_linked(conn, event)
        # Anything else (incl. merge / split, owned by PersonsProjection):
        # not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``person_identities`` table."""
        conn.execute(delete(person_identities_table))

    # ------------------------------------------------------------------ helpers

    def _apply_linked(self, conn: Connection, event: IdentityLinked) -> None:
        """Upsert one identity row keyed on ``(connector, handle)``.

        ``person_id`` / ``display`` / ``confidence`` are refreshed on
        conflict so the latest :class:`IdentityLinked` wins — the
        resolver re-emits a link (not a merge) when it finds an exact
        match, so the conflict path re-parents the identity onto the
        matched person deterministically. ``linked_at`` is kept at the
        first-seen value so the binding's age survives re-observation.
        """
        stmt = sqlite_insert(person_identities_table).values(
            connector=event.connector,
            handle=event.handle,
            person_id=event.aggregate_id,
            display=event.display,
            confidence=event.confidence,
            linked_at=event.occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["connector", "handle"],
            set_={
                "person_id": stmt.excluded.person_id,
                "display": stmt.excluded.display,
                "confidence": stmt.excluded.confidence,
            },
        )
        conn.execute(stmt)
