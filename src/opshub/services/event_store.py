"""Event store Protocol + in-memory implementation.

The :class:`EventStore` Protocol is the seam between the service layer and the
physical persistence layer. Phase 1 step 9 ships only the in-memory
implementation so :class:`opshub.services.task_service.TaskService` can be unit
tested without depending on the SQLAlchemy engine or the ``events`` table
schema (which lands in steps 7 / 10).

Design notes:

- ``@runtime_checkable`` so tests can assert structural conformance with
  ``isinstance(store, EventStore)``.
- The Protocol is intentionally minimal: a single ``append`` method. Read /
  replay APIs land alongside the projection store in step 10 — exposing them
  now would freeze a shape we have not yet exercised end-to-end.
- :class:`InMemoryEventStore` returns a *defensive copy* from
  :meth:`InMemoryEventStore.events` so callers cannot mutate the internal log
  through the snapshot. Append order is preserved (list semantics).
- The optional ``connection`` argument on :meth:`EventStore.append` is what
  lets the service layer commit an event append and a projection apply in the
  same Unit of Work. In-memory implementations ignore it; the SQLAlchemy
  implementation routes the insert through the caller's connection so the
  caller controls commit / rollback.
- Thread safety is out of scope for Phase 1: the CLI is single-process and
  step 10's SQLAlchemy-backed store will provide its own transactional
  guarantees through the :class:`~opshub.db.UnitOfWork`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from opshub.domain.events import DomainEvent

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


@runtime_checkable
class EventStore(Protocol):
    """Append-only sink for domain events.

    Implementations may persist to memory (tests), SQLite via SQLAlchemy
    (step 10), or any other store; the service layer only sees this Protocol.
    """

    def append(self, event: DomainEvent, connection: Connection | None = None) -> None:
        """Append ``event`` to the store. Must preserve insertion order.

        ``connection`` lets the caller bind the append to an existing
        Unit of Work. Implementations that don't speak SQLAlchemy
        (in-memory, in-process queues) may ignore the argument.
        """
        ...


class InMemoryEventStore:
    """In-memory :class:`EventStore` used by service-layer unit tests.

    The store keeps an internal ``list[DomainEvent]`` in append order. It is
    *not* a drop-in replacement for the SQLAlchemy store at runtime: it has no
    durability, no transactional semantics, and no projection coupling. It
    exists so the service contract can be exercised without spinning up the
    database.
    """

    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def append(self, event: DomainEvent, connection: Connection | None = None) -> None:
        """Append ``event`` to the internal log.

        ``DomainEvent`` is frozen (Pydantic ``model_config.frozen=True``) so we
        can store the instance directly without copying — the caller cannot
        mutate it after the fact.

        ``connection`` is accepted for Protocol conformance and ignored —
        the in-memory store has no database transaction to join.
        """
        _ = connection
        self._events.append(event)

    @property
    def events(self) -> list[DomainEvent]:
        """Return a snapshot of the appended events in insertion order.

        Returns a shallow copy so test callers (or any reader) cannot mutate
        the internal log through the returned list. Each ``DomainEvent`` is
        itself immutable, so a shallow copy is sufficient.
        """
        return list(self._events)
