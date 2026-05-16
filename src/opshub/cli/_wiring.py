"""Shared CLI wiring helpers.

The CLI's subcommand modules each open their own SQLAlchemy ``Engine`` and
share two preconditions:

1. Settings must resolve from the standard ``OPSHUB_*`` env vars / config
   file via :class:`opshub.core.config.OpsHubSettings`.
2. The SQLite database must already contain the ``events`` table — i.e.
   ``opshub init`` (or ``opshub db migrate``) must have run first. Running
   a subcommand against an uninitialised database is a configuration
   mistake, not a runtime fault, so it surfaces as
   :class:`opshub.core.errors.ConfigError`.

Centralising both steps here keeps every subcommand identical and removes
the temptation to duplicate the inspector / engine boilerplate across
``projections``, ``embeddings``, ``task`` etc.

Module-level imports stay limited to ``__future__`` plus ``TYPE_CHECKING``
shims (ADR-0001 lazy-import rule); the heavy SQLAlchemy / pydantic_settings
imports happen inside :func:`build_engine`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from opshub.domain.events import DomainEvent
    from opshub.projections import Projection
    from opshub.services import TaskService


__all__ = ["build_engine", "build_task_service"]


def build_engine() -> Engine:
    """Construct the OpsHub SQLAlchemy ``Engine`` for CLI subcommands.

    Resolves :class:`OpsHubSettings` from the environment, builds the
    SQLite engine via :func:`create_engine_for_sqlite`, and asserts the
    schema has been initialised. Subcommands call this exactly once per
    invocation.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.core.config import OpsHubSettings
    from opshub.db import create_engine_for_sqlite

    settings = OpsHubSettings()
    engine = create_engine_for_sqlite(settings.storage.db_path)
    _require_initialised(engine)
    return engine


def _require_initialised(engine: Engine) -> None:
    """Raise :class:`ConfigError` when the OpsHub schema is missing.

    Detection key: the ``events`` table is provisioned by migration
    ``0001_create_events_table`` and is required by every subcommand
    that reads or replays the event log. Absence of that table is a
    reliable proxy for "user has not run ``opshub init`` yet".
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from sqlalchemy import inspect

    from opshub.core.errors import ConfigError

    if "events" not in inspect(engine).get_table_names():
        raise ConfigError("OpsHub DB is not initialised; run `opshub init` first.")


def build_task_service(actor: str) -> TaskService:
    """Wire a :class:`TaskService` against the configured database.

    The returned service appends events to the SQLite-backed
    :class:`~opshub.db.SqlAlchemyEventStore` *and* projects them inline
    through a :class:`_PersistingProjector` that writes to every
    registered :class:`~opshub.projections.Projection` in the same
    database. Both writes share a single transaction opened via
    ``engine.begin()`` — a failure in either rolls back both, so the
    event log and the projections can never disagree.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import TaskService

    engine = build_engine()
    return TaskService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        actor=actor,
        # ``engine.begin()`` returns a context manager that yields the
        # active ``Connection`` and commits on clean exit / rolls back
        # on exception — exactly the contract :class:`TaskService`
        # expects from ``uow_factory``.
        uow_factory=engine.begin,
    )


class _PersistingProjector:
    """Apply events to every registered projection on a shared connection.

    The projector reads the projection list from
    :func:`opshub.projections.all_projections` so the CLI wiring and the
    ``projections rebuild`` driver always see the same set of
    projections — no second hard-coded list to drift.

    Each call fans the event out to every projection on the connection
    supplied by the service. The service opens a single transaction via
    its ``uow_factory`` and threads that connection in, so all
    projections (and the underlying event row) participate in the same
    Unit of Work.
    """

    def __init__(self) -> None:
        # Lazy import: keep CLI cold start fast (ADR-0001).
        from opshub.projections import all_projections

        self._projections: list[Projection] = all_projections()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        """Apply ``event`` to every registered projection on ``connection``.

        ``connection`` is required in the CLI wiring path — the service
        threads in the UoW connection so all projection writes share
        the event-append transaction. Falling back to ``None`` would
        silently undo the atomicity guarantee, so we raise instead.
        """
        if connection is None:
            raise RuntimeError(
                "_PersistingProjector requires a Connection from the service's"
                " uow_factory; received None"
            )
        for projection in self._projections:
            projection.apply(connection, event)
