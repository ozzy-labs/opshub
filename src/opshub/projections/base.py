"""Projection Protocol.

A :class:`Projection` reduces an ordered stream of
:class:`~opshub.domain.events.DomainEvent` into a read-model table. The
Protocol intentionally takes a live SQLAlchemy ``Connection`` (not an
``Engine``) so that the rebuild driver can compose multiple projector
applies into a single transaction — projections must never open their own
transactions, since that would break rebuild atomicity.

Idempotency contract: ``reset(conn)`` must leave the projection's tables
empty so that the rebuild driver can replay every event from scratch with
deterministic results. Projections that store derived data outside of their
own tables (e.g. external caches) are out of scope for Phase 1.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy.engine import Connection

from opshub.domain.events import DomainEvent


@runtime_checkable
class Projection(Protocol):
    """Apply domain events to a read model.

    Implementations declare a stable ``name`` (used by operational tooling
    and logging) and a pair of write hooks that must commute over the
    Connection passed in by the rebuild driver.
    """

    name: str
    """Stable, human-readable identifier (e.g. ``"tasks"``).

    Used in logs and CLI output. Does not need to match the underlying
    table name, but the Phase 1 convention is to keep them aligned.
    """

    def apply(self, conn: Connection, event: DomainEvent) -> None:
        """Apply ``event`` to the read-model table(s) via ``conn``.

        Must not commit or roll back ``conn``. Implementations should
        ignore events they do not recognise (the rebuild driver fans every
        event out to every projection; each projection filters by
        ``event_type`` internally).
        """
        ...

    def reset(self, conn: Connection) -> None:
        """Empty every read-model table owned by this projection.

        Called by the rebuild driver *before* replay so that a partial
        previous projection state cannot bleed into the new snapshot.
        Implementations typically issue ``DELETE FROM <table>``; a
        ``TRUNCATE`` would skip the integrity checks SQLite relies on.
        """
        ...
