"""Tests for :class:`opshub.services.auto_embed_hook.AutoEmbedHook` and the
``EmbeddingService.embed_one_if_pending`` single-row entry point (Phase 5
step C1).

Two layers of test live here:

1. Pure-Python unit tests for :class:`AutoEmbedHook` driven by a stub
   :class:`EmbeddingService` that just records calls / raises. These
   pin the event-type → entity-type dispatch table and the
   "never raises" contract.
2. Integration-style tests for
   :meth:`EmbeddingService.embed_one_if_pending` against a migrated
   SQLite engine with a scripted embedder + vector store. These pin
   the "NOT EXISTS filter against current backend's embedding row"
   semantics and the "empty text returns False without recording"
   semantics. The fixture + stub shapes mirror
   :mod:`tests.unit.services.test_embedding_service` so the
   reviewer can compare patterns side-by-side.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.core.time import now_utc
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.domain.events import (
    DecisionRecorded,
    EmbeddingFailed,
    ItemEnqueued,
    LockAcquired,
    SourceObserved,
    TaskCreated,
    TextEmbedded,
)
from opshub.projections import all_projections
from opshub.projections.tasks import tasks_table
from opshub.services.auto_embed_hook import AutoEmbedHook
from opshub.services.embedding_service import EmbeddingService
from opshub.services.event_hook import EventHook
from opshub.services.projector import NoOpProjector
from opshub.vectors.embedder import EmbeddingResult
from opshub.vectors.store import RecallHit, StoredEmbedding

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from opshub.domain.events import DomainEvent


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


class _PersistingProjectorForTests:
    """Mini stand-in for the CLI ``_PersistingProjector`` (private).

    Iterates ``all_projections()`` and applies each event to every
    registered projection on the provided connection. Used by the
    Layer-3 integration tests that need ``TaskService.create_task`` to
    write into the ``tasks`` projection so the auto-embed hook can
    read from it.
    """

    def __init__(self) -> None:
        self._projections = all_projections()

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        if connection is None:
            raise RuntimeError("test projector requires a Connection")
        for projection in self._projections:
            projection.apply(connection, event)


# ---------------------------------------------------------------------------
# Layer 1: AutoEmbedHook dispatch tests with a stub EmbeddingService
# ---------------------------------------------------------------------------


class _StubEmbeddingService:
    """Stub recording :meth:`embed_one_if_pending` invocations.

    Only the attributes :class:`AutoEmbedHook` touches are present —
    the hook never calls the rebuild path, so we do not stub it.
    """

    def __init__(self, *, raise_on: tuple[str, ...] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raise_on = raise_on or ()

    def embed_one_if_pending(self, entity_type: str, entity_id: str) -> bool:
        self.calls.append((entity_type, entity_id))
        if entity_type in self._raise_on:
            raise RuntimeError(f"simulated embed failure for {entity_type}")
        return True


def _task_event(*, aggregate_id: str | None = None, title: str = "t") -> TaskCreated:
    return TaskCreated(
        aggregate_id=aggregate_id or new_ulid(),
        actor="cli:test",
        title=title,
    )


def _decision_event(*, aggregate_id: str | None = None) -> DecisionRecorded:
    return DecisionRecorded(
        aggregate_id=aggregate_id or new_ulid(),
        actor="cli:test",
        text="we will use SQLite",
    )


def _inbox_event(*, aggregate_id: str | None = None) -> ItemEnqueued:
    return ItemEnqueued(
        aggregate_id=aggregate_id or new_ulid(),
        actor="cli:test",
        summary="captured note",
    )


def _source_event(*, aggregate_id: str | None = None) -> SourceObserved:
    return SourceObserved(
        aggregate_id=aggregate_id or new_ulid(),
        actor="connector:test",
        connector_name="github",
        external_id="owner/repo#42",
        source_type="issue",
        title="example issue",
        # epic #470 / issue #481: ``body`` is required + non-empty.
        body="example issue body",
    )


def _lock_event() -> LockAcquired:
    return LockAcquired(
        aggregate_id=new_ulid(),
        actor="cli:test",
        scope_type="task",
        scope_id=new_ulid(),
    )


def test_event_hook_protocol_is_satisfied_by_auto_embed_hook() -> None:
    """:class:`AutoEmbedHook` is a structural :class:`EventHook`."""
    stub = _StubEmbeddingService()
    # ``EmbeddingService`` type — we satisfy the constructor signature
    # via duck typing because :class:`AutoEmbedHook` uses
    # ``TYPE_CHECKING`` for the parameter annotation.
    hook = AutoEmbedHook(stub)  # type: ignore[arg-type]
    assert isinstance(hook, EventHook)


def test_auto_embed_hook_calls_embed_one_if_pending_for_task_created() -> None:
    """``task.created`` → embed_one_if_pending('task', aggregate_id)."""
    stub = _StubEmbeddingService()
    hook = AutoEmbedHook(stub)  # type: ignore[arg-type]
    event = _task_event()

    hook.maybe_embed(event)

    assert stub.calls == [("task", event.aggregate_id)]


def test_auto_embed_hook_calls_embed_one_if_pending_for_decision_recorded() -> None:
    """``decision.recorded`` → ('decision', aggregate_id)."""
    stub = _StubEmbeddingService()
    hook = AutoEmbedHook(stub)  # type: ignore[arg-type]
    event = _decision_event()

    hook.maybe_embed(event)

    assert stub.calls == [("decision", event.aggregate_id)]


def test_auto_embed_hook_calls_embed_one_if_pending_for_inbox_enqueued() -> None:
    """``inbox.enqueued`` → ('inbox_item', aggregate_id)."""
    stub = _StubEmbeddingService()
    hook = AutoEmbedHook(stub)  # type: ignore[arg-type]
    event = _inbox_event()

    hook.maybe_embed(event)

    assert stub.calls == [("inbox_item", event.aggregate_id)]


def test_auto_embed_hook_calls_embed_one_if_pending_for_source_observed() -> None:
    """``source.observed`` → ('source', aggregate_id)."""
    stub = _StubEmbeddingService()
    hook = AutoEmbedHook(stub)  # type: ignore[arg-type]
    event = _source_event()

    hook.maybe_embed(event)

    assert stub.calls == [("source", event.aggregate_id)]


def test_auto_embed_hook_ignores_unrelated_events() -> None:
    """Lock / triage / state-transition events are no-ops on the hook."""
    stub = _StubEmbeddingService()
    hook = AutoEmbedHook(stub)  # type: ignore[arg-type]

    hook.maybe_embed(_lock_event())

    assert stub.calls == []


def test_auto_embed_hook_swallows_embed_failures() -> None:
    """When the embedding service raises, the hook still returns None."""
    stub = _StubEmbeddingService(raise_on=("task",))
    hook = AutoEmbedHook(stub)  # type: ignore[arg-type]
    event = _task_event()

    # No exception escapes; recorded call attempt is preserved.
    hook.maybe_embed(event)

    assert stub.calls == [("task", event.aggregate_id)]


# ---------------------------------------------------------------------------
# Layer 2: EmbeddingService.embed_one_if_pending integration tests
# ---------------------------------------------------------------------------


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "auto_embed_hook.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


class _StubEmbedder:
    """Embedder stub matching ``tests/unit/services/test_embedding_service``."""

    def __init__(
        self,
        *,
        model_id: str = "stub-embedder",
        model_version: str = "v1",
        dim: int = 4,
        fail_on: str | None = None,
    ) -> None:
        self._model_id = model_id
        self._model_version = model_version
        self._dim = dim
        self._fail_on = fail_on
        self.calls: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> EmbeddingResult:
        self.calls.append(text)
        if self._fail_on is not None and text == self._fail_on:
            raise RuntimeError("stub embed failure")
        base = float(len(text) % self._dim) / max(self._dim, 1)
        return EmbeddingResult(
            vector=tuple(base + i * 0.1 for i in range(self._dim)),
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


class _RecordingVectorStore:
    """VectorStore stub that writes the ``embeddings`` metadata row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.upserted: list[StoredEmbedding] = []

    def upsert(self, embeddings: list[StoredEmbedding]) -> None:
        with self._engine.begin() as conn:
            for emb in embeddings:
                conn.execute(
                    text(
                        "INSERT INTO embeddings "
                        "(entity_type, entity_id, model_id, model_version, "
                        " dim, created_at) VALUES "
                        "(:et, :eid, :mid, :mv, :dim, :ts)"
                    ),
                    {
                        "et": emb.entity_type,
                        "eid": emb.entity_id,
                        "mid": emb.model_id,
                        "mv": emb.model_version,
                        "dim": len(emb.vector),
                        "ts": emb.created_at,
                    },
                )
        self.upserted.extend(embeddings)

    def recall(
        self,
        query: tuple[float, ...],
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:  # pragma: no cover
        del query, k, entity_types
        return []

    def recall_by_rowid(
        self,
        entity_type: str,
        entity_id: str,
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:  # pragma: no cover
        del entity_type, entity_id, k, entity_types
        return []

    def count(self, *, entity_type: str | None = None) -> int:  # pragma: no cover
        del entity_type
        return 0

    def delete(self, *, entity_type: str, entity_id: str) -> int:  # pragma: no cover
        del entity_type, entity_id
        return 0


def _seed_task(engine: Engine, *, title: str | None) -> str:
    """Insert one ``tasks`` row; return the freshly minted ULID."""
    task_id = new_ulid()
    now = now_utc()
    # ``tasks.title`` is NOT NULL in the projection schema, so a
    # truly-empty test case writes an empty string rather than NULL.
    title_value = title if title is not None else ""
    with engine.begin() as conn:
        conn.execute(
            insert(tasks_table).values(
                id=task_id,
                title=title_value,
                body=None,
                state="draft",
                result_note=None,
                created_at=now,
                updated_at=now,
            )
        )
    return task_id


def _make_service(
    engine: Engine,
    *,
    embedder: _StubEmbedder | None = None,
    vector_store: _RecordingVectorStore | None = None,
) -> tuple[EmbeddingService, _StubEmbedder, _RecordingVectorStore]:
    embedder = embedder or _StubEmbedder()
    vector_store = vector_store or _RecordingVectorStore(engine)
    service = EmbeddingService(
        store=SqlAlchemyEventStore(engine),
        projector=NoOpProjector(),
        embedder=embedder,
        vector_store=vector_store,
        engine=engine,
        uow_factory=engine.begin,
    )
    return service, embedder, vector_store


def test_embed_one_if_pending_returns_true_for_new_entity(
    migrated_engine: Engine,
) -> None:
    """A pending task → embed_one_if_pending writes a vector + returns True."""
    service, embedder, vector_store = _make_service(migrated_engine)
    task_id = _seed_task(migrated_engine, title="fresh task")

    result = service.embed_one_if_pending("task", task_id)

    assert result is True
    assert embedder.calls == ["fresh task"]
    assert [(s.entity_type, s.entity_id) for s in vector_store.upserted] == [("task", task_id)]


def test_embed_one_if_pending_returns_false_when_already_embedded(
    migrated_engine: Engine,
) -> None:
    """A pre-embedded task → returns False, embedder never called (idempotent)."""
    service, embedder, vector_store = _make_service(migrated_engine)
    task_id = _seed_task(migrated_engine, title="already done")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO embeddings (entity_type, entity_id, model_id, "
                "model_version, dim, created_at) VALUES "
                "('task', :eid, :mid, :mv, 4, :ts)"
            ),
            {
                "eid": task_id,
                "mid": embedder.model_id,
                "mv": embedder.model_version,
                "ts": now_utc(),
            },
        )

    result = service.embed_one_if_pending("task", task_id)

    assert result is False
    assert embedder.calls == []
    assert vector_store.upserted == []


def test_embed_one_if_pending_returns_false_for_empty_text(
    migrated_engine: Engine,
) -> None:
    """An entity with empty / whitespace title → False, no event appended."""
    service, embedder, _ = _make_service(migrated_engine)
    empty_id = _seed_task(migrated_engine, title="")
    whitespace_id = _seed_task(migrated_engine, title="   ")

    # ``tasks.title`` field validator on TaskCreated rejects empty strings,
    # so we use the projection-side state directly: the embedder must not
    # be called.
    assert service.embed_one_if_pending("task", empty_id) is False
    assert service.embed_one_if_pending("task", whitespace_id) is False
    assert embedder.calls == []
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            select(text("COUNT(*) FROM events WHERE event_type = 'embedding.failed'"))
        ).scalar()
    assert rows == 0


def test_embed_one_if_pending_returns_false_for_unknown_entity_id(
    migrated_engine: Engine,
) -> None:
    """A missing entity_id → False without raising."""
    service, embedder, _ = _make_service(migrated_engine)

    result = service.embed_one_if_pending("task", new_ulid())

    assert result is False
    assert embedder.calls == []


def test_embed_one_if_pending_returns_false_for_unknown_entity_type(
    migrated_engine: Engine,
) -> None:
    """An entity_type outside the supported set → False without raising."""
    service, embedder, _ = _make_service(migrated_engine)

    result = service.embed_one_if_pending("nope", "01HZZZZZZZZZZZZZZZZZZZZZZA")

    assert result is False
    assert embedder.calls == []


def test_embed_one_if_pending_records_failure_event_on_embed_error(
    migrated_engine: Engine,
) -> None:
    """Embedder raises → EmbeddingFailed event is appended; method returns False."""
    embedder = _StubEmbedder(fail_on="poison row")
    service, _, vector_store = _make_service(migrated_engine, embedder=embedder)
    task_id = _seed_task(migrated_engine, title="poison row")

    result = service.embed_one_if_pending("task", task_id)

    assert result is False
    assert vector_store.upserted == []
    # The failure event was recorded.
    from opshub.db.schema import events_table

    with migrated_engine.connect() as conn:
        rows = conn.execute(
            select(events_table).where(events_table.c.event_type == "embedding.failed")
        ).all()
    assert len(rows) == 1


def test_embed_one_if_pending_never_raises_even_on_failure(
    migrated_engine: Engine,
) -> None:
    """The contract: embed_one_if_pending never raises (post-commit hook safety)."""
    embedder = _StubEmbedder(fail_on="boom")
    service, _, _ = _make_service(migrated_engine, embedder=embedder)
    task_id = _seed_task(migrated_engine, title="boom")

    # No pytest.raises wrapper — the call must complete normally.
    result = service.embed_one_if_pending("task", task_id)
    assert result is False


# ---------------------------------------------------------------------------
# Layer 3: TaskService + AutoEmbedHook end-to-end integration
# ---------------------------------------------------------------------------


def test_task_service_with_auto_embed_hook_populates_embeddings_projection(
    migrated_engine: Engine,
) -> None:
    """``TaskService.create_task`` with the hook → ``embeddings`` row appears.

    Exercises the wiring contract: a service that receives an
    :class:`AutoEmbedHook` in ``event_hooks`` runs the hook **after**
    its UoW commits. Verifies the projection state changes — the
    ``embeddings`` row is the externally observable proof that the
    hook fired, distinct from the in-memory call recording used by
    Layer 1.
    """
    from opshub.services import TaskService

    embedder = _StubEmbedder()
    vector_store = _RecordingVectorStore(migrated_engine)
    embedding_service = EmbeddingService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_PersistingProjectorForTests(),
        embedder=embedder,
        vector_store=vector_store,
        engine=migrated_engine,
        uow_factory=migrated_engine.begin,
    )
    hook = AutoEmbedHook(embedding_service)
    task_service = TaskService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_PersistingProjectorForTests(),
        actor="cli:test",
        uow_factory=migrated_engine.begin,
        event_hooks=(hook,),
    )

    created = task_service.create_task(title="auto-embed me")

    # The hook must have driven a single embed_one call for this task.
    assert embedder.calls == ["auto-embed me"]
    assert [(s.entity_type, s.entity_id) for s in vector_store.upserted] == [
        ("task", created.aggregate_id)
    ]
    # The ``embeddings`` projection now has a row for the task.
    from opshub.db.schema import events_table

    with migrated_engine.connect() as conn:
        embedded_events = conn.execute(
            select(events_table).where(events_table.c.event_type == "embedding.text_embedded")
        ).all()
    assert len(embedded_events) == 1


def test_task_service_without_hooks_does_not_embed(
    migrated_engine: Engine,
) -> None:
    """Default ``event_hooks=None`` → no embedding side-effect (Phase 4 behaviour)."""
    from opshub.services import TaskService

    task_service = TaskService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_PersistingProjectorForTests(),
        actor="cli:test",
        uow_factory=migrated_engine.begin,
    )

    task_service.create_task(title="no hook")

    # No ``TextEmbedded`` event was appended (no embedder, no hook).
    from opshub.db.schema import events_table

    with migrated_engine.connect() as conn:
        embedded_events = conn.execute(
            select(events_table).where(events_table.c.event_type == "embedding.text_embedded")
        ).all()
    assert embedded_events == []


def test_task_service_hook_failure_does_not_unwind_originating_event(
    migrated_engine: Engine,
) -> None:
    """A hook that raises → the ``task.created`` event still committed."""
    from opshub.services import TaskService

    class _BrokenHook:
        def maybe_embed(self, event: object) -> None:
            raise RuntimeError("hook is broken")

    task_service = TaskService(
        store=SqlAlchemyEventStore(migrated_engine),
        projector=_PersistingProjectorForTests(),
        actor="cli:test",
        uow_factory=migrated_engine.begin,
        event_hooks=(_BrokenHook(),),
    )

    created = task_service.create_task(title="durable")

    # The originating event committed despite the hook exception.
    from opshub.db.schema import events_table

    with migrated_engine.connect() as conn:
        rows = conn.execute(
            select(events_table).where(events_table.c.event_type == "task.created")
        ).all()
    assert len(rows) == 1
    assert created.title == "durable"


# Helper to silence the unused-import lint when no domain event is
# actually instantiated in some tests (``EmbeddingFailed`` /
# ``TextEmbedded`` are imported as a doc cue).
_ = (EmbeddingFailed, TextEmbedded)
