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
    from opshub.services import (
        DecisionService,
        HandoffService,
        InboxService,
        LockService,
        TaskService,
    )


__all__ = [
    "build_decision_service",
    "build_engine",
    "build_handoff_service",
    "build_inbox_service",
    "build_lock_service",
    "build_task_service",
]


def build_engine() -> Engine:
    """Construct the OpsHub SQLAlchemy ``Engine`` for CLI subcommands."""
    from opshub.core.config import OpsHubSettings
    from opshub.db import create_engine_for_sqlite

    settings = OpsHubSettings()
    engine = create_engine_for_sqlite(settings.storage.db_path)
    _require_initialised(engine)
    return engine


def _require_initialised(engine: Engine) -> None:
    """Raise :class:`ConfigError` when the OpsHub schema is missing."""
    from sqlalchemy import inspect

    from opshub.core.errors import ConfigError

    if "events" not in inspect(engine).get_table_names():
        raise ConfigError("OpsHub DB is not initialised; run `opshub init` first.")


def build_task_service(actor: str) -> TaskService:
    """Wire a :class:`TaskService` against the configured database."""
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import TaskService

    engine = build_engine()
    return TaskService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        actor=actor,
        uow_factory=engine.begin,
    )


def build_inbox_service(actor: str) -> InboxService:
    """Wire an :class:`InboxService` against the configured database."""
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import InboxService

    engine = build_engine()
    return InboxService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        uow_factory=engine.begin,
        actor=actor,
    )


def build_lock_service(actor: str) -> LockService:
    """Wire a :class:`LockService` against the configured database."""
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import LockService

    engine = build_engine()
    return LockService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        uow_factory=engine.begin,
        actor=actor,
    )


def build_decision_service(actor: str) -> DecisionService:
    """Wire a :class:`DecisionService` against the configured database."""
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import DecisionService

    engine = build_engine()
    return DecisionService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        actor=actor,
        uow_factory=engine.begin,
    )


def build_handoff_service(actor: str) -> HandoffService:
    """Wire a :class:`HandoffService` against the configured database.

    Parallels :func:`build_task_service`: the returned service shares
    a single transaction across event append and projection apply via
    ``engine.begin``, and the engine is also stashed on the service so
    :meth:`HandoffService.list_open` can read the ``handoffs``
    projection through the same connection pool.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import HandoffService

    engine = build_engine()
    return HandoffService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        actor=actor,
        uow_factory=engine.begin,
        engine=engine,
    )


class _PersistingProjector:
    """Apply events to every registered projection on a shared connection."""

    def __init__(self) -> None:
        from opshub.projections import all_projections

        self._projections: list[Projection] = all_projections()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        if connection is None:
            raise RuntimeError(
                "_PersistingProjector requires a Connection from the service's"
                " uow_factory; received None"
            )
        for projection in self._projections:
            projection.apply(connection, event)
