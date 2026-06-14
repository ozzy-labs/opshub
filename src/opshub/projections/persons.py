"""``persons`` read-model projection (Phase 25-B, ADR-0043).

The ``persons`` table is the canonical read model for the person
aggregate — one human who shows up across connectors under different
author handles. It is the entity the ``person:<id>`` graph ref
(ADR-0017 §改訂) and the 秘書化 v1 commitment ledger (25-C) point at.

This projection owns the ``persons`` table and additionally applies the
two **cross-table** person-axis events (:class:`IdentityMerged` /
:class:`IdentitySplit`), which mutate both ``persons`` and
``person_identities``. Keeping those two-table mutations inside a single
projection ``apply`` makes each one atomic and order-independent — there
is no inter-projection ordering hazard on the ``person_id`` FK that a
split-across-two-projections design would create (merge needs identities
re-parented *before* the person is deleted; split needs the new person
inserted *before* the identity is repointed — conflicting orderings if
each table had its own reducer).

Event handling:

* :class:`PersonIdentified` → ``INSERT ... ON CONFLICT(id) DO NOTHING``.
  A fresh person row keyed on its ULID. The conflict path is a no-op so
  re-applying on rebuild is idempotent.
* :class:`IdentityMerged` → re-parent every ``person_identities`` row
  from ``merged_person_id`` onto the surviving ``aggregate_id``, bump the
  survivor's ``updated_at``, then delete the merged person row. Order
  matters *within* this handler (re-parent first so the FK never
  dangles) but not across projections.
* :class:`IdentitySplit` → INSERT the new person (``new_person_id``) then
  repoint the ``(identity_connector, identity_handle)`` row onto it. The
  INSERT-before-repoint order keeps the ``person_id`` FK satisfied.

Column shape mirrors migration ``0035_create_persons_table`` (1:1). The
:data:`persons_table` :class:`~sqlalchemy.Table` is registered on the
shared :data:`opshub.db.schema.metadata` at import time so Alembic
autogenerate sees it symmetrically with ``person_identities``.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    delete,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from opshub.db.schema import metadata
from opshub.domain.events import (
    DomainEvent,
    IdentityMerged,
    IdentitySplit,
    PersonIdentified,
)
from opshub.projections.person_identities import person_identities_table

__all__ = ["PersonsProjection", "persons_table"]


persons_table: Table = Table(
    "persons",
    metadata,
    Column("id", String(length=26), primary_key=True),
    Column("display_name", Text(), nullable=False),
    Column("is_operator", Integer(), nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_persons_is_operator", "is_operator"),
)
"""SQLAlchemy ``Table`` mirroring migration ``0035_create_persons_table``."""


class PersonsProjection:
    """Reducer mapping person-axis events to ``persons`` rows (ADR-0043).

    Owns the ``persons`` table and applies the two cross-table merge /
    split events atomically (see module docstring). Each statement runs
    on the Connection passed in by the rebuild driver / service UoW —
    the projection never opens its own transaction (see
    :class:`~opshub.projections.base.Projection`).
    """

    name = "persons"

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Dispatch ``event`` to one of the three person-axis paths."""
        if isinstance(event, PersonIdentified):
            self._apply_identified(conn, event)
        elif isinstance(event, IdentityMerged):
            self._apply_merged(conn, event)
        elif isinstance(event, IdentitySplit):
            self._apply_split(conn, event)
        # IdentityLinked is owned by PersonIdentitiesProjection; anything
        # else: not our concern.

    def reset(self, conn: Connection) -> None:
        """Empty the ``persons`` table."""
        conn.execute(delete(persons_table))

    # ------------------------------------------------------------------ helpers

    def _apply_identified(self, conn: Connection, event: PersonIdentified) -> None:
        """Insert a fresh person row (no-op on id conflict for rebuild idempotency)."""
        stmt = sqlite_insert(persons_table).values(
            id=event.aggregate_id,
            display_name=event.display_name,
            is_operator=1 if event.is_operator else 0,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
        conn.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))

    def _apply_merged(self, conn: Connection, event: IdentityMerged) -> None:
        """Re-parent identities onto the survivor, bump it, drop the merged person.

        Re-parent runs **before** the delete so the ``person_id`` FK on
        ``person_identities`` never dangles (and the ``ON DELETE
        CASCADE`` cannot wrongly sweep the identities away). The whole
        sequence is one projection ``apply`` so it stays atomic inside
        the rebuild / service transaction.
        """
        survivor = event.aggregate_id
        merged = event.merged_person_id
        conn.execute(
            update(person_identities_table)
            .where(person_identities_table.c.person_id == merged)
            .values(person_id=survivor)
        )
        conn.execute(
            update(persons_table)
            .where(persons_table.c.id == survivor)
            .values(updated_at=event.occurred_at)
        )
        conn.execute(delete(persons_table).where(persons_table.c.id == merged))

    def _apply_split(self, conn: Connection, event: IdentitySplit) -> None:
        """Mint the new person, then repoint the identity onto it.

        INSERT-before-repoint keeps the ``person_id`` FK satisfied. The
        new person inherits the detached identity's ``display`` as its
        ``display_name`` when available (resolved by the service before
        the event is emitted); the projection keeps it simple and uses
        the handle as the display name fallback so a replayed split is
        deterministic without re-reading ``person_identities``.
        """
        # Look up the identity's current display so the new person row has
        # a meaningful recognition cue; falls back to the handle.
        row = conn.execute(
            person_identities_table.select().where(
                (person_identities_table.c.connector == event.identity_connector)
                & (person_identities_table.c.handle == event.identity_handle)
            )
        ).first()
        display = (
            row.display if row is not None and row.display else None
        ) or event.identity_handle

        stmt = sqlite_insert(persons_table).values(
            id=event.new_person_id,
            display_name=display,
            is_operator=0,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
        conn.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
        conn.execute(
            update(person_identities_table)
            .where(
                (person_identities_table.c.connector == event.identity_connector)
                & (person_identities_table.c.handle == event.identity_handle)
            )
            .values(person_id=event.new_person_id, confidence="manual")
        )
