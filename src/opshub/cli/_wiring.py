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
        DuplicateService,
        EmbeddingService,
        EventHook,
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
    "build_duplicate_service",
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
    """Wire a :class:`TaskService` against the configured database.

    When ``[embedding] auto = true`` and a real backend is configured,
    an :class:`~opshub.services.auto_embed_hook.AutoEmbedHook` is
    injected as a post-commit event hook (Phase 5 step C1) so
    ``task.created`` events auto-embed without a manual
    ``opshub embeddings rebuild``.
    """
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import TaskService

    engine = build_engine()
    return TaskService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        actor=actor,
        uow_factory=engine.begin,
        event_hooks=_maybe_build_auto_embed_hooks(engine),
    )


def build_inbox_service(actor: str) -> InboxService:
    """Wire an :class:`InboxService` against the configured database.

    See :func:`build_task_service` for the Phase 5 auto-embed hook
    semantics.
    """
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import InboxService

    engine = build_engine()
    return InboxService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        uow_factory=engine.begin,
        actor=actor,
        event_hooks=_maybe_build_auto_embed_hooks(engine),
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
    """Wire a :class:`DecisionService` against the configured database.

    See :func:`build_task_service` for the Phase 5 auto-embed hook
    semantics.
    """
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import DecisionService

    engine = build_engine()
    return DecisionService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        actor=actor,
        uow_factory=engine.begin,
        event_hooks=_maybe_build_auto_embed_hooks(engine),
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
    # Phase 5 step C1: SourceService.observe appends a SourceObserved
    # and an ItemEnqueued in the same UoW. We pass the auto-embed
    # hook to BOTH services so each event runs through it after the
    # shared UoW commits — InboxService.enqueue is never called
    # (SourceService inlines the inbox event), but plumbing the hook
    # symmetrically keeps the wiring graph consistent for future
    # connector paths that might call InboxService directly.
    hooks = _maybe_build_auto_embed_hooks(engine)
    inbox = InboxService(
        store=store,
        projector=projector,
        uow_factory=engine.begin,
        actor=actor,
        event_hooks=hooks,
    )
    return SourceService(
        store=store,
        projector=projector,
        inbox_service=inbox,
        uow_factory=engine.begin,
        actor=actor,
        engine=engine,
        event_hooks=hooks,
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
    # Phase 5 step C1: workspace ingest emits ItemEnqueued events; the
    # auto-embed hook applies to those just like connector-origin
    # inbox rows. FileIngestService itself does not (yet) accept an
    # event_hooks kwarg — its ItemEnqueued event is built inline, so
    # the post-commit dispatch happens via the InboxService reference
    # the service holds (mirroring SourceService composition).
    hooks = _maybe_build_auto_embed_hooks(engine)
    inbox = InboxService(
        store=store,
        projector=projector,
        uow_factory=engine.begin,
        actor=actor,
        event_hooks=hooks,
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


def build_duplicate_service() -> DuplicateService:
    """Wire a :class:`DuplicateService` for the configured engine + backend.

    Resolves the active :class:`~opshub.vectors.embedder.Embedder` +
    :class:`~opshub.vectors.store.VectorStore` via the Phase 4 factory
    (PR #68); the CLI ``opshub embeddings find-duplicates`` subcommand
    is the only caller in Phase 4 MVP. Mirrors
    :func:`build_embedding_service` so backend switches via config
    take effect on the next invocation without restarting any
    long-lived process.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001). The factory
    # itself defers the heavy embedder import to the branch the
    # operator selected (see :mod:`opshub.vectors.factory`).
    from opshub.core.config import OpsHubSettings
    from opshub.services import DuplicateService
    from opshub.vectors.factory import build_embedder, build_vector_store

    settings = OpsHubSettings()
    engine = build_engine()
    return DuplicateService(
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


def _maybe_build_auto_embed_hooks(engine: Engine) -> tuple[EventHook, ...]:
    """Return ``(AutoEmbedHook,)`` when auto-embed is enabled, else ``()``.

    Phase 5 step C1 introduces an opt-in projector hook that calls
    :meth:`EmbeddingService.embed_one_if_pending` after a service
    commits an embeddable event. The hook is wired into every
    service that emits embeddable events (TaskService /
    DecisionService / InboxService / SourceService) so the operator
    only needs to flip ``[embedding] auto = true`` once.

    Default behaviour (``auto = false``) returns an empty tuple, so
    no hook is registered and the services run identically to Phase
    4. When ``auto = true`` but ``backend = "disabled"``, the hook
    cannot do anything useful (no embedder to call), so we also
    return an empty tuple — failing loud here would be hostile to
    operators who flipped the flag before installing an extras
    bundle.

    Building the hook involves constructing a transient
    :class:`EmbeddingService` against the same engine + active
    backend. The service is cheap to instantiate (factories defer
    heavy embedder loading until first use), but the hook holds a
    reference to it so subsequent ``maybe_embed`` calls reuse the
    same embedder + vector store — important for backends that
    cache model state (e.g. local sentence-transformer).
    """
    from opshub.core.config import OpsHubSettings
    from opshub.db import SqlAlchemyEventStore
    from opshub.services import AutoEmbedHook, EmbeddingService
    from opshub.vectors.factory import build_embedder, build_vector_store

    settings = OpsHubSettings()
    if not settings.embedding.auto or settings.embedding.backend == "disabled":
        return ()
    embedding_service = EmbeddingService(
        store=SqlAlchemyEventStore(engine),
        projector=_PersistingProjector(),
        embedder=build_embedder(settings),
        vector_store=build_vector_store(settings, engine),
        engine=engine,
        uow_factory=engine.begin,
        actor="hook:auto_embed",
    )
    return (AutoEmbedHook(embedding_service),)


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
