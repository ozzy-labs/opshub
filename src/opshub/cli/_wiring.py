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
        AgentRunService,
        DecisionService,
        EmbeddingService,
        FileIngestService,
        HandoffService,
        InboxService,
        LockService,
        RecallService,
        SourceService,
        TaskService,
        WorkSessionService,
    )


__all__ = [
    "build_agent_run_service",
    "build_decision_service",
    "build_embedding_service",
    "build_engine",
    "build_file_ingest_service",
    "build_handoff_service",
    "build_inbox_service",
    "build_lock_service",
    "build_recall_service",
    "build_session_service",
    "build_source_service",
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


def build_session_service(actor: str) -> WorkSessionService:
    """Wire a :class:`WorkSessionService` against the configured database.

    Parallels :func:`build_handoff_service`: a single transaction wraps
    event append and projection apply via ``engine.begin``, and the
    engine is also stashed on the service so :meth:`list_active` can
    read the ``work_sessions`` projection through the same connection
    pool.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import WorkSessionService

    engine = build_engine()
    return WorkSessionService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        uow_factory=engine.begin,
        actor=actor,
        engine=engine,
    )


def build_agent_run_service(actor: str) -> AgentRunService:
    """Wire an :class:`AgentRunService` against the configured database.

    Mirrors :func:`build_session_service`.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import AgentRunService

    engine = build_engine()
    return AgentRunService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        uow_factory=engine.begin,
        actor=actor,
        engine=engine,
    )


def build_source_service(actor: str = "connector:source") -> SourceService:
    """Wire a :class:`SourceService` against the configured database.

    The :class:`InboxService` shares the same engine, projector
    instance class, and ``uow_factory`` so :class:`SourceObserved` and
    :class:`ItemEnqueued` commit in a single transaction (see
    :mod:`opshub.services.source_service` module docstring for the
    atomic shape rationale). The same ``actor`` is threaded into both
    services so source-driven inbox rows carry connector provenance
    identical to the source event.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import InboxService, SourceService

    engine = build_engine()
    store = SqlAlchemyEventStore(engine)
    projector = _PersistingProjector()
    inbox = InboxService(
        store=store,
        projector=projector,
        uow_factory=engine.begin,
        actor=actor,
    )
    return SourceService(
        store=store,
        projector=projector,
        inbox_service=inbox,
        uow_factory=engine.begin,
        actor=actor,
        engine=engine,
    )


def build_file_ingest_service(actor: str = "cli:workspace_ingest") -> FileIngestService:
    """Wire a :class:`FileIngestService` against the configured database.

    Modelled on :func:`build_source_service`: the
    :class:`InboxService` reference is held purely for composition
    bookkeeping (the inbox-side :class:`ItemEnqueued` event is built
    inline by :class:`FileIngestService` so the shared UoW stays
    intact, mirroring :class:`SourceService.observe`). The same engine,
    projector instance class, and ``uow_factory`` are threaded into
    both services so any future cross-service refactor finds a
    well-formed wiring graph already in place.

    The engine is passed explicitly to :class:`FileIngestService` so
    :meth:`FileIngestService.ingest_inbox_dir` can read the
    ``ingested_files`` projection — the projection lookup is what
    makes the workspace ingest path idempotent across runs.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import FileIngestService, InboxService

    engine = build_engine()
    store = SqlAlchemyEventStore(engine)
    projector = _PersistingProjector()
    inbox = InboxService(
        store=store,
        projector=projector,
        uow_factory=engine.begin,
        actor=actor,
    )
    return FileIngestService(
        store=store,
        projector=projector,
        inbox_service=inbox,
        engine=engine,
        uow_factory=engine.begin,
        actor=actor,
    )


def build_embedding_service(actor: str = "cli:embeddings_rebuild") -> EmbeddingService:
    """Wire an :class:`EmbeddingService` for the configured engine + backend.

    Resolves the active :class:`~opshub.vectors.embedder.Embedder` +
    :class:`~opshub.vectors.store.VectorStore` via the Phase 4 factory
    (PR #68), then constructs the service with the shared engine +
    ``engine.begin`` UoW. The caller is the
    ``opshub embeddings rebuild`` CLI subcommand (PR B3); resolving the
    embedder lazily here means config changes (backend switch) take
    effect on the next invocation without restarting any long-lived
    process.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001). The factory
    # itself defers the heavy embedder import to the branch the
    # operator selected (see :mod:`opshub.vectors.factory`).
    from opshub.core.config import OpsHubSettings
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import EmbeddingService
    from opshub.vectors.factory import build_embedder, build_vector_store

    settings = OpsHubSettings()
    engine = build_engine()
    return EmbeddingService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        embedder=build_embedder(settings),
        vector_store=build_vector_store(settings, engine),
        engine=engine,
        uow_factory=engine.begin,
        actor=actor,
    )


def build_recall_service() -> RecallService:
    """Wire a :class:`RecallService` for the active backend.

    Resolves the :class:`~opshub.vectors.embedder.Embedder` +
    :class:`~opshub.vectors.store.VectorStore` via the Phase 4 factory
    (PR #68), then constructs the service with the shared engine. No
    ``actor`` parameter — recall is a read-only query path, no events
    are appended.

    Mirrors :func:`build_embedding_service` for backend resolution so
    a config change (backend switch) takes effect on the next
    ``opshub recall`` invocation without restarting any long-lived
    process.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001). The factory
    # itself defers the heavy embedder import to the branch the
    # operator selected (see :mod:`opshub.vectors.factory`).
    from opshub.core.config import OpsHubSettings
    from opshub.services import RecallService
    from opshub.vectors.factory import build_embedder, build_vector_store

    settings = OpsHubSettings()
    engine = build_engine()
    return RecallService(
        embedder=build_embedder(settings),
        vector_store=build_vector_store(settings, engine),
        engine=engine,
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
