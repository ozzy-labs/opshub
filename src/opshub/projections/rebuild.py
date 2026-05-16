"""Projection rebuild driver.

Rebuilding a projection means: empty its tables, then replay every event
in the store in ``recorded_at, id`` order, fanning each event out to every
registered projection. This is the operational escape hatch when a
projection schema changes or when a projection bug needs to be corrected
without touching the event log (ADR-0002 — the event log is the source
of truth, projections are disposable).

The driver wraps the entire rewind+replay in a single transaction so a
mid-replay crash leaves the previous projection state intact. This trades
peak memory (one transaction across the whole event stream) for atomicity;
Phase 1's event volume is well below the SQLite write-ahead-log limits.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from sqlalchemy.engine import Engine

from opshub.domain.events import DomainEvent
from opshub.projections.base import Projection


@runtime_checkable
class ReplayableEventStore(Protocol):
    """Event store that supports full-stream iteration for rebuilds.

    The base :class:`opshub.services.event_store.EventStore` Protocol only
    requires ``append`` because services do not need to read back; the
    rebuild driver needs ``iter_all`` and so declares its own narrower
    Protocol here rather than widening the service-layer contract.
    """

    def iter_all(self) -> Iterator[DomainEvent]:
        """Yield every event in the store in canonical replay order."""
        ...


def rebuild_all(
    engine: Engine,
    store: ReplayableEventStore,
    projections: list[Projection],
) -> None:
    """Reset every projection then replay all events in order.

    Implementation notes:

    * ``engine.begin()`` opens one transaction for the whole operation.
      Commit happens on exit if no exception is raised; an exception
      triggers a rollback so a partial rebuild can never replace a valid
      previous projection state.
    * Resets run before replay so that two consecutive ``rebuild_all``
      calls produce byte-identical projection rows — this is the
      idempotency property the integration test pins down.
    * Each event is fanned out to *every* projection; individual
      projections filter by ``event_type`` internally. This keeps the
      driver oblivious to the projection schema.
    """
    with engine.begin() as conn:
        for projection in projections:
            projection.reset(conn)

        for event in store.iter_all():
            for projection in projections:
                projection.apply(conn, event)
