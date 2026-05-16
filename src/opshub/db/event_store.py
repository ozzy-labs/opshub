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
  payload through the :data:`~opshub.domain.events.AllEvent`
  discriminated union. Unknown event types raise
  :class:`opshub.core.errors.OpsHubError` so corrupted rows surface
  loudly rather than silently dropping data.

Transaction story (after the Phase 2 prep refactor):

The :meth:`SqlAlchemyEventStore.append` API takes an optional
``connection`` argument. When the caller supplies one, the insert
participates in the caller's transaction — that is how
:class:`opshub.services.task_service.TaskService` keeps the event
append and the projection apply in the same Unit of Work. When no
connection is passed, the store falls back to its own short-lived
:class:`UnitOfWork`, preserving the historical per-event durability
contract for callers that don't need cross-component atomicity.

Timestamps stored in ``DateTime(timezone=True)`` columns round-trip as
tz-aware UTC datetimes because the column type tells SQLAlchemy to
attach UTC tzinfo on read; the Pydantic ``AfterValidator`` on
``DomainEvent`` re-validates this on rehydration.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import insert, select
from sqlalchemy.engine import Connection, Engine

from opshub.core.errors import OpsHubError
from opshub.db.schema import events_table
from opshub.db.unit_of_work import UnitOfWork
from opshub.domain.events import AllEvent, DomainEvent

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["SqlAlchemyEventStore"]


# Discriminated-union adapter — a module-level singleton so the validation
# schema is built once. ``AllEvent`` aliases the union of every event
# family OpsHub knows how to decode. New event families plug in by
# extending :data:`AllEvent` in ``opshub.domain.events`` — no edits here
# required.
_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)


class SqlAlchemyEventStore:
    """Persistent :class:`~opshub.services.event_store.EventStore`.

    Two transaction modes are supported:

    * **Caller-managed** (preferred for command pipelines):
      :meth:`append` accepts a ``connection`` argument. The insert runs
      on the caller's :class:`~sqlalchemy.engine.Connection` and is
      committed (or rolled back) as part of the caller's wider Unit of
      Work. This is the mode :class:`opshub.services.task_service.TaskService`
      uses so the event append and the projection apply commit
      atomically.
    * **Self-managed** (legacy / single-shot writes): when ``connection``
      is ``None``, :meth:`append` opens its own :class:`UnitOfWork` and
      commits per call. Existing callers and tests get the same
      durability semantics they had before the refactor.

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

    def append(self, event: DomainEvent, connection: Connection | None = None) -> None:
        """Persist ``event`` as one row in the ``events`` table.

        The Pydantic ``model_dump_json`` call serialises the full event
        (including the discriminator and any subclass payload) so replay
        can reconstruct the original instance via
        :data:`~opshub.domain.events.AllEvent`. Only routing-relevant
        fields are also lifted into their own columns for indexed access.

        Parameters
        ----------
        event:
            The event to persist.
        connection:
            Optional caller-supplied connection. When provided, the
            insert runs on the caller's transaction and the caller is
            responsible for ``commit`` / ``rollback``. When ``None``,
            the store opens its own UoW and commits per call.
        """
        payload = event.model_dump_json()
        statement = insert(events_table).values(
            id=event.event_id,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=payload,
            schema_version=event.schema_version,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            actor=event.actor,
        )
        if connection is not None:
            connection.execute(statement)
            return
        with self._uow_factory() as uow:
            uow.execute(statement)
            uow.commit()

    def iter_all(self) -> Iterator[DomainEvent]:
        """Yield every event in canonical replay order.

        Ordering is ``recorded_at, id`` (the ULID id is monotonic per
        millisecond, so it tie-breaks events recorded in the same wall
        clock instant deterministically).

        Unknown ``event_type`` values raise :class:`OpsHubError` — the
        discriminated-union adapter validates each row against every
        known event family, and silently dropping an unknown type would
        let projection rebuilds quietly diverge from the source of
        truth.
        """
        statement = select(events_table.c.event_type, events_table.c.payload).order_by(
            events_table.c.recorded_at.asc(), events_table.c.id.asc()
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

        Dispatches through the :data:`AllEvent` discriminated union;
        pydantic picks the right subclass via the ``event_type`` field.

        An unrecognised ``event_type`` surfaces as a pydantic
        :class:`~pydantic.ValidationError` (no matching union member).
        We wrap it in :class:`OpsHubError` with a version-neutral
        message so an event written by a newer build is diagnosed as
        "binary too old" rather than "Phase X only handles ...".
        """
        try:
            return _AllEventAdapter.validate_json(payload)
        except PydanticValidationError as exc:
            raise OpsHubError(
                f"unknown event_type {event_type!r}; this opshub binary may be outdated"
            ) from exc
