"""SQLAlchemy-backed :class:`~opshub.services.event_store.EventStore`.

Phase 1's persistence story for the event log: one row per
:class:`~opshub.domain.events.DomainEvent`, stored verbatim as JSON in the
``payload`` column with the routing-relevant fields lifted out into typed
columns so SQL queries (projection rebuild, ad-hoc replay) don't have to
parse JSON.

Round-trip semantics:

* Append: serialise the event with ``model_dump_json`` and write
  ``(event_id, aggregate_id, event_type, schema_version, occurred_at,
  recorded_at, actor, payload)``.
* Replay: read rows in ``recorded_at, id`` order and rehydrate each
  payload through the :data:`~opshub.domain.events.TaskEvent`
  discriminated union. Unknown event types raise
  :class:`opshub.core.errors.OpsHubError` so corrupted rows surface
  loudly rather than silently dropping data.

Timestamps stored in ``DateTime(timezone=True)`` columns round-trip as
tz-aware UTC datetimes because the column type tells SQLAlchemy to
attach UTC tzinfo on read; the Pydantic ``AfterValidator`` on
``DomainEvent`` re-validates this on rehydration.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlalchemy import Table, insert, select
from sqlalchemy.engine import Engine

from opshub.core.errors import OpsHubError
from opshub.db.schema import metadata
from opshub.db.unit_of_work import UnitOfWork
from opshub.domain.events import DomainEvent, TaskEvent

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["SqlAlchemyEventStore"]


# Discriminated-union adapter — a module-level singleton so the validation
# schema is built once. ``TaskEvent`` is the Phase 1 union; future event
# families will extend it (or move to a dispatcher keyed by ``event_type``
# prefix).
_TaskEventAdapter: TypeAdapter[TaskEvent] = TypeAdapter(TaskEvent)


def _events_table() -> Table:
    """Look up the autoloaded ``events`` table on the shared metadata.

    The table is provisioned by migration ``0001_create_events_table``.
    Looking it up by name (rather than declaring a separate ``Table``
    object) keeps this module aligned with the migration even if the
    ``opshub.db.schema`` module later attaches a typed ``Table`` for the
    same name.
    """
    if "events" in metadata.tables:
        return metadata.tables["events"]
    # Lazily declare the Core ``Table`` reflecting the migration shape so
    # this module is usable without going through ``MetaData.reflect``.
    # We import inside the function to avoid a hard dependency at import
    # time on SQLAlchemy column types that the rest of the package may
    # not need.
    from sqlalchemy import Column, DateTime, Integer, String, Text

    return Table(
        "events",
        metadata,
        Column("id", String(length=26), primary_key=True),
        Column("aggregate_id", String(), nullable=False),
        Column("event_type", String(), nullable=False),
        Column("payload", Text(), nullable=False),
        Column("schema_version", Integer(), nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("recorded_at", DateTime(timezone=True), nullable=False),
        Column("actor", String(), nullable=False),
    )


class SqlAlchemyEventStore:
    """Persistent :class:`~opshub.services.event_store.EventStore`.

    Each ``append`` opens its own :class:`~opshub.db.UnitOfWork` so the
    service layer can stay oblivious to transaction management while still
    getting per-event durability. Callers that need batch appends inside
    a wider transaction can replace the UoW factory through the
    ``uow_factory`` constructor argument.

    Parameters
    ----------
    engine:
        SQLAlchemy engine bound to the OpsHub SQLite database.
    uow_factory:
        Callable returning a :class:`UnitOfWork`. Defaults to
        ``lambda: UnitOfWork(engine)``; tests can pass a factory that
        joins an outer transaction.
    """

    def __init__(
        self,
        engine: Engine,
        uow_factory: Callable[[], UnitOfWork] | None = None,
    ) -> None:
        self._engine = engine
        self._uow_factory: Callable[[], UnitOfWork] = (
            uow_factory if uow_factory is not None else (lambda: UnitOfWork(engine))
        )
        self._events_tbl: Table = _events_table()

    def append(self, event: DomainEvent) -> None:
        """Persist ``event`` as one row in the ``events`` table.

        The Pydantic ``model_dump_json`` call serialises the full event
        (including the discriminator and any subclass payload) so replay
        can reconstruct the original instance via
        :data:`~opshub.domain.events.TaskEvent`. Only routing-relevant
        fields are also lifted into their own columns for indexed access.
        """
        payload = event.model_dump_json()
        statement = insert(self._events_tbl).values(
            id=event.event_id,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=payload,
            schema_version=event.schema_version,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            actor=event.actor,
        )
        with self._uow_factory() as uow:
            uow.execute(statement)
            uow.commit()

    def iter_all(self) -> Iterator[DomainEvent]:
        """Yield every event in canonical replay order.

        Ordering is ``recorded_at, id`` (the ULID id is monotonic per
        millisecond, so it tie-breaks events recorded in the same wall
        clock instant deterministically).

        Unknown ``event_type`` values raise :class:`OpsHubError` — Phase
        1 only knows about task events, and silently dropping an unknown
        type would let projection rebuilds quietly diverge from the
        source of truth.
        """
        tbl = self._events_tbl
        statement = select(tbl.c.event_type, tbl.c.payload).order_by(
            tbl.c.recorded_at.asc(), tbl.c.id.asc()
        )
        with self._engine.connect() as conn:
            result = conn.execute(statement)
            for row in result:
                event_type: str = row.event_type
                payload: str = row.payload
                yield self._decode(event_type, payload)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _decode(event_type: str, payload: str) -> DomainEvent:
        """Rehydrate ``payload`` into a concrete :class:`DomainEvent`.

        Currently routes through the :data:`TaskEvent` discriminated
        union; future event families will extend the dispatch.
        """
        if not event_type.startswith("task."):
            raise OpsHubError(f"unknown event_type {event_type!r}; Phase 1 only handles task.*")
        try:
            return _TaskEventAdapter.validate_json(payload)
        except Exception as exc:  # pragma: no cover - defensive
            raise OpsHubError(
                f"failed to decode event payload for event_type {event_type!r}"
            ) from exc
