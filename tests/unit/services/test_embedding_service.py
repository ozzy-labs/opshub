"""Tests for :class:`opshub.services.embedding_service.EmbeddingService`.

The unit suite drives the service through a migrated SQLite engine
(the same ``migrated_engine`` fixture pattern used by
:mod:`tests.unit.services.test_file_ingest_service`) because the
service needs to read the per-entity projection tables AND the
``embeddings`` metadata table on every rebuild. A stub
:class:`~opshub.vectors.embedder.Embedder` returns deterministic fake
vectors so the test never depends on a real model; a stub
:class:`~opshub.vectors.store.VectorStore` records ``upsert`` calls
**and** writes a minimal ``embeddings`` row so the next pass of
``embed_pending`` correctly sees that entity as "already embedded".

Atomicity is verified end-to-end against the migrated SQLite engine —
a failing projector mid-``_commit_embedding`` must roll back the
``TextEmbedded`` event row. The VectorStore stub asserts that no
upsert ran past the rollback point.
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
from opshub.db.schema import events_table
from opshub.domain.events import (
    DomainEvent,
    EmbeddingFailed,
    EmbeddingRebuildRequested,
    TextEmbedded,
)
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table
from opshub.services.embedding_service import EmbeddingService
from opshub.services.projector import NoOpProjector
from opshub.vectors.embedder import Embedder, EmbeddingResult
from opshub.vectors.store import RecallHit, StoredEmbedding, VectorStore

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from opshub.services.projector import Projector


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


# ---- fixtures + stubs -----------------------------------------------------


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied.

    The upgrade path includes migration 0013 (PR #64) which drops
    ``embeddings.vector`` and creates the ``embeddings_vec_*`` virtual
    tables. The ``[vector]`` extras must be installed (sqlite-vec) for
    the migration to succeed; CI runs ``uv sync --extra vector`` for
    this reason.
    """
    db_path = tmp_path / "embedding_service.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


class _StubEmbedder:
    """Embedder stub that returns deterministic vectors.

    The vector is derived from ``len(text)`` so each input maps to a
    distinct (but stable) value — enough for the service to record an
    embedding without touching a real model. The :meth:`fail_on` knob
    lets tests trigger an exception on a specific input text.
    """

    def __init__(
        self,
        *,
        model_id: str = "stub-embedder",
        model_version: str = "v1",
        dim: int = 4,
        fail_on: str | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._model_id = model_id
        self._model_version = model_version
        self._dim = dim
        self._fail_on = fail_on
        self._fail_with = fail_with or RuntimeError("stub embed failure")
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
            raise self._fail_with
        # Fake vector: each component encodes the text length so two
        # different texts produce different vectors.
        base = float(len(text) % self._dim) / max(self._dim, 1)
        return EmbeddingResult(
            vector=tuple(base + i * 0.1 for i in range(self._dim)),
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


class _RecordingVectorStore:
    """VectorStore stub that records upserts and writes the metadata row.

    Writing a row into the ``embeddings`` metadata table is what makes
    the LEFT-JOIN / NOT EXISTS filter in :meth:`EmbeddingService._iter_pending`
    see the entity as "already embedded" on the next pass. The stub
    skips the ``embeddings_vec_*`` insert because the unit tests do not
    exercise recall — that path is covered in the integration suite.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.upserted: list[StoredEmbedding] = []
        self.deleted_targets: list[tuple[str, str]] = []

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
    ) -> list[RecallHit]:  # pragma: no cover - unused
        del query, k, entity_types
        return []

    def recall_by_rowid(
        self,
        entity_type: str,
        entity_id: str,
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:  # pragma: no cover - unused
        del entity_type, entity_id, k, entity_types
        return []

    def count(self, *, entity_type: str | None = None) -> int:  # pragma: no cover
        del entity_type
        return 0

    def delete(self, *, entity_type: str, entity_id: str) -> int:
        # Phase 10 step B2: ``EmbeddingService.purge_embeddings``
        # invokes ``VectorStore.delete`` for every embedded entity it
        # purges and sums its return value into the purge total. The
        # stub mirrors the canonical
        # :class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore`
        # delete shape: drop the metadata row(s) via the same engine
        # the upsert path wrote them on, return the rowcount. The
        # vec0 virtual tables are unused in this suite — see the
        # integration tests for that lock-step.
        self.deleted_targets.append((entity_type, entity_id))
        with self._engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM embeddings WHERE entity_type = :et AND entity_id = :eid"),
                {"et": entity_type, "eid": entity_id},
            )
            return int(result.rowcount or 0)


class _FailingVectorStore:
    """VectorStore stub that always raises on upsert."""

    def __init__(self) -> None:
        self.upsert_calls = 0

    def upsert(self, embeddings: list[StoredEmbedding]) -> None:
        del embeddings
        self.upsert_calls += 1
        raise RuntimeError("simulated vector-store upsert failure")

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


class _FailingProjector:
    """Projector that raises on ``apply`` for non-bracket events.

    The bracketing :class:`EmbeddingRebuildRequested` event must still
    commit (otherwise the test setup never enters the
    ``_commit_embedding`` path under test). The :class:`TextEmbedded`
    apply call is the one that should roll back.
    """

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = connection
        if isinstance(event, TextEmbedded):
            raise RuntimeError("simulated projector failure")
        # Other event families (rebuild requested, failed) pass through.


def _seed_task(engine: Engine, *, title: str) -> str:
    """Insert one :data:`tasks_table` row in the ``draft`` state.

    Returns the freshly minted ULID so the test can assert against it.
    """
    task_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(tasks_table).values(
                id=task_id,
                title=title,
                body=None,
                state="draft",
                result_note=None,
                created_at=now,
                updated_at=now,
            )
        )
    return task_id


def _seed_decision(engine: Engine, *, text_value: str) -> str:
    """Insert one :data:`decisions_table` row."""
    decision_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(decisions_table).values(
                id=decision_id,
                text=text_value,
                context=None,
                actor="cli:test",
                recorded_at=now,
            )
        )
    return decision_id


def _seed_inbox_item(engine: Engine, *, summary: str) -> str:
    """Insert one :data:`inbox_items_table` row in the ``pending`` state."""
    item_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(inbox_items_table).values(
                id=item_id,
                summary=summary,
                source_ref=None,
                state="pending",
                disposition=None,
                target_id=None,
                reason=None,
                created_at=now,
                updated_at=now,
            )
        )
    return item_id


def _seed_source(
    engine: Engine,
    *,
    summary: str | None,
    external_id: str,
    body: str | None = None,
) -> str:
    """Insert one :data:`sources_table` row.

    Phase 10 step B2: the optional ``body`` kwarg defaults to None so
    every legacy test path keeps inserting summary-only rows. New
    Phase 10 tests pass ``body`` explicitly to exercise the
    ``COALESCE(body, summary)`` fallback in
    :class:`EmbeddingService._iter_pending`.
    """
    source_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name="github",
                external_id=external_id,
                source_type="issue",
                title="placeholder title",
                url=None,
                summary=summary,
                observed_at=now,
                updated_at=now,
                body=body,
            )
        )
    return source_id


def _seed_existing_embedding(
    engine: Engine,
    *,
    entity_type: str,
    entity_id: str,
    model_id: str,
    model_version: str,
    dim: int = 4,
) -> None:
    """Write one row into the ``embeddings`` metadata table.

    Simulates a previous successful embed for the given
    ``(model_id, model_version)`` so the JOIN filter in
    :meth:`EmbeddingService._iter_pending` excludes the row.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO embeddings "
                "(entity_type, entity_id, model_id, model_version, "
                " dim, created_at) VALUES "
                "(:et, :eid, :mid, :mv, :dim, :ts)"
            ),
            {
                "et": entity_type,
                "eid": entity_id,
                "mid": model_id,
                "mv": model_version,
                "dim": dim,
                "ts": now_utc(),
            },
        )


def _make_service(
    engine: Engine,
    *,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
    projector: Projector | None = None,
) -> EmbeddingService:
    """Build an :class:`EmbeddingService` against the migrated engine.

    ``uow_factory`` is always ``engine.begin`` — the service requires
    a real engine for the projection reads, so the in-memory
    InMemoryEventStore path is not exercised.
    """
    return EmbeddingService(
        store=SqlAlchemyEventStore(engine),
        projector=projector if projector is not None else NoOpProjector(),
        embedder=embedder if embedder is not None else _StubEmbedder(),
        vector_store=vector_store if vector_store is not None else _RecordingVectorStore(engine),
        engine=engine,
        uow_factory=engine.begin,
    )


def _events_of_type(engine: Engine, event_type: str) -> list[DomainEvent]:
    """Decode every persisted event of ``event_type`` via the event store.

    The :class:`SqlAlchemyEventStore` does not expose a read-back API
    in Phase 1 (the event store is append-only); for assertion
    purposes we query the raw table and rebuild the model from the
    JSON payload.
    """
    import json

    from pydantic import TypeAdapter

    from opshub.domain.events import AllEvent

    adapter: TypeAdapter[DomainEvent] = TypeAdapter(AllEvent)
    with engine.connect() as conn:
        rows = conn.execute(
            select(events_table).where(events_table.c.event_type == event_type)
        ).all()
    decoded: list[DomainEvent] = []
    for row in rows:
        payload = json.loads(row.payload)
        decoded.append(adapter.validate_python(payload))
    return decoded


# ---- embed_pending --------------------------------------------------------


def test_embed_pending_with_empty_db_returns_zero_counts(migrated_engine: Engine) -> None:
    """No entities → embedded=0, skipped=0, failed=0; one rebuild event."""
    service = _make_service(migrated_engine)

    result = service.embed_pending()

    assert result.embedded_count == 0
    assert result.skipped_count == 0
    assert result.failed_count == 0
    assert len(result.rebuild_run_id) == 26, "rebuild_run_id must be a ULID"

    rebuilds = _events_of_type(migrated_engine, "embedding.rebuild_requested")
    assert len(rebuilds) == 1
    assert isinstance(rebuilds[0], EmbeddingRebuildRequested)
    assert rebuilds[0].aggregate_id == result.rebuild_run_id


def test_embed_pending_embeds_only_pending_rows(migrated_engine: Engine) -> None:
    """3 fresh + 1 pre-embedded task → embedded=3, skipped=0, failed=0."""
    embedder = _StubEmbedder()
    seeded = [_seed_task(migrated_engine, title=f"task {i}") for i in range(3)]
    pre_embedded = _seed_task(migrated_engine, title="already done")
    _seed_existing_embedding(
        migrated_engine,
        entity_type="task",
        entity_id=pre_embedded,
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="task")

    assert result.embedded_count == 3
    assert result.skipped_count == 0
    assert result.failed_count == 0
    # The pre-embedded task was excluded by the NOT EXISTS filter.
    assert pre_embedded not in embedder.calls
    embedded_events = _events_of_type(migrated_engine, "embedding.text_embedded")
    assert {e.aggregate_id for e in embedded_events} == set(seeded)


def test_embed_pending_skips_rows_with_empty_text(migrated_engine: Engine) -> None:
    """A row with NULL ``summary`` (source) is reported as skipped, no event."""
    embedder = _StubEmbedder()
    _seed_source(migrated_engine, summary=None, external_id="repo#null-summary")
    _seed_source(migrated_engine, summary="   ", external_id="repo#whitespace")
    _seed_source(migrated_engine, summary="real summary", external_id="repo#ok")

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="source")

    assert result.embedded_count == 1
    assert result.skipped_count == 2
    assert result.failed_count == 0
    embedded_events = _events_of_type(migrated_engine, "embedding.text_embedded")
    assert len(embedded_events) == 1


def test_count_pending_matches_scope_and_drops_to_zero_after_embed(
    migrated_engine: Engine,
) -> None:
    """``count_pending`` counts only un-embedded rows; zero after a rebuild."""
    embedder = _StubEmbedder()
    for i in range(3):
        _seed_task(migrated_engine, title=f"task {i}")
    pre_embedded = _seed_task(migrated_engine, title="already done")
    _seed_existing_embedding(
        migrated_engine,
        entity_type="task",
        entity_id=pre_embedded,
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )
    service = _make_service(migrated_engine, embedder=embedder)

    # Same NOT EXISTS scope as embed_pending: the pre-embedded row is excluded.
    assert service.count_pending(entity_type="task") == 3

    service.embed_pending(entity_type="task")
    assert service.count_pending(entity_type="task") == 0


def test_embed_pending_invokes_progress_callback_once_per_processed_row(
    migrated_engine: Engine,
) -> None:
    """The callback fires once per row processed (embedded + skipped alike)."""
    embedder = _StubEmbedder()
    _seed_source(migrated_engine, summary=None, external_id="repo#null")
    _seed_source(migrated_engine, summary="   ", external_id="repo#ws")
    _seed_source(migrated_engine, summary="real summary", external_id="repo#ok")
    service = _make_service(migrated_engine, embedder=embedder)

    ticks: list[int] = []
    result = service.embed_pending(entity_type="source", progress_callback=ticks.append)

    # 1 embedded + 2 skipped = 3 rows processed = 3 callback ticks.
    assert result.embedded_count == 1
    assert result.skipped_count == 2
    assert ticks == [1, 1, 1]


def test_embed_pending_records_failure_event_and_continues(
    migrated_engine: Engine,
) -> None:
    """Embedder failure on one row → ``EmbeddingFailed`` event, batch continues."""
    embedder = _StubEmbedder(fail_on="poison row")
    good_id = _seed_task(migrated_engine, title="harmless")
    bad_id = _seed_task(migrated_engine, title="poison row")

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="task")

    assert result.embedded_count == 1
    assert result.failed_count == 1
    # Both rows were attempted (no fail-fast).
    assert sorted(embedder.calls) == sorted(["harmless", "poison row"])

    failed_events = _events_of_type(migrated_engine, "embedding.failed")
    assert len(failed_events) == 1
    failed = failed_events[0]
    assert isinstance(failed, EmbeddingFailed)
    assert failed.entity_id == bad_id
    assert failed.entity_type == "task"
    # The good row produced exactly one TextEmbedded event.
    embedded_events = _events_of_type(migrated_engine, "embedding.text_embedded")
    assert len(embedded_events) == 1
    assert embedded_events[0].aggregate_id == good_id


def test_embed_pending_with_entity_type_filter_restricts_scope(
    migrated_engine: Engine,
) -> None:
    """``entity_type='task'`` embeds tasks only; sources are untouched."""
    embedder = _StubEmbedder()
    task_ids = [_seed_task(migrated_engine, title=f"task {i}") for i in range(2)]
    source_ids = [
        _seed_source(migrated_engine, summary=f"source {i}", external_id=f"repo#{i}")
        for i in range(2)
    ]

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="task")

    assert result.embedded_count == 2
    embedded_events = _events_of_type(migrated_engine, "embedding.text_embedded")
    assert all(e.entity_type == "task" for e in embedded_events)  # type: ignore[attr-defined]
    assert {e.entity_id for e in embedded_events} == set(task_ids)  # type: ignore[attr-defined]
    # Sources were not embedded.
    assert all(s not in [e.entity_id for e in embedded_events] for s in source_ids)  # type: ignore[attr-defined]


def test_embed_pending_with_limit_stops_after_n(migrated_engine: Engine) -> None:
    """``limit=2`` embeds two rows; the rest remain pending for a future run."""
    embedder = _StubEmbedder()
    [_seed_task(migrated_engine, title=f"task {i}") for i in range(5)]

    service = _make_service(migrated_engine, embedder=embedder)
    first_run = service.embed_pending(entity_type="task", limit=2)
    assert first_run.embedded_count == 2

    # A follow-up rebuild without limit picks up the remaining 3.
    second_run = service.embed_pending(entity_type="task")
    assert second_run.embedded_count == 3


def test_text_embedded_event_payload_matches_embedder_metadata(
    migrated_engine: Engine,
) -> None:
    """The ``TextEmbedded`` event records model_id / model_version / dim."""
    embedder = _StubEmbedder(model_id="custom-id", model_version="v42", dim=8)
    _seed_task(migrated_engine, title="payload check")

    service = _make_service(migrated_engine, embedder=embedder)
    service.embed_pending(entity_type="task")

    embedded_events = _events_of_type(migrated_engine, "embedding.text_embedded")
    assert len(embedded_events) == 1
    event = embedded_events[0]
    assert isinstance(event, TextEmbedded)
    assert event.model_id == "custom-id"
    assert event.model_version == "v42"
    assert event.dim == 8


def test_rebuild_request_event_records_scope_and_model(
    migrated_engine: Engine,
) -> None:
    """``EmbeddingRebuildRequested`` captures scope + active model."""
    embedder = _StubEmbedder(model_id="scope-test", model_version="v9")
    service = _make_service(migrated_engine, embedder=embedder)

    full = service.embed_pending()
    scoped = service.embed_pending(entity_type="task")

    rebuilds = _events_of_type(migrated_engine, "embedding.rebuild_requested")
    by_run = {e.aggregate_id: e for e in rebuilds}
    full_event = by_run[full.rebuild_run_id]
    scoped_event = by_run[scoped.rebuild_run_id]
    assert isinstance(full_event, EmbeddingRebuildRequested)
    assert full_event.scope == "all"
    assert full_event.model_id == "scope-test"
    assert full_event.model_version == "v9"
    assert isinstance(scoped_event, EmbeddingRebuildRequested)
    assert scoped_event.scope == "task"


def test_atomicity_failing_projector_rolls_back_text_embedded(
    migrated_engine: Engine,
) -> None:
    """A projector failure on TextEmbedded rolls back the event row.

    The VectorStore.upsert call runs LAST inside the UoW, so a
    projector failure unwinds before the vector landed. The
    ``embeddings`` metadata row count must therefore remain at zero,
    and no TextEmbedded event should be visible in the event log.
    """
    embedder = _StubEmbedder()
    recording_store = _RecordingVectorStore(migrated_engine)
    _seed_task(migrated_engine, title="will not persist")

    service = _make_service(
        migrated_engine,
        embedder=embedder,
        vector_store=recording_store,
        projector=_FailingProjector(),
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.embed_pending(entity_type="task")

    # The rebuild bracket succeeds (the failing projector lets non-Text
    # events pass) but the TextEmbedded event itself rolls back, so the
    # subsequent VectorStore.upsert is never called.
    rebuilds = _events_of_type(migrated_engine, "embedding.rebuild_requested")
    assert len(rebuilds) == 1
    embedded_events = _events_of_type(migrated_engine, "embedding.text_embedded")
    assert embedded_events == []
    # The vector store never saw an upsert — the projector failure
    # raises inside the UoW, before the upsert sequencing point.
    assert recording_store.upserted == []


def test_atomicity_failing_projector_propagates_runtime_error(
    migrated_engine: Engine,
) -> None:
    """Programmatic check: the projector exception escapes ``embed_pending``."""
    embedder = _StubEmbedder()
    _seed_task(migrated_engine, title="boom")

    service = _make_service(
        migrated_engine,
        embedder=embedder,
        projector=_FailingProjector(),
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.embed_pending(entity_type="task")


def test_atomicity_failing_vector_store_leaves_event_for_retry(
    migrated_engine: Engine,
) -> None:
    """A :meth:`VectorStore.upsert` failure propagates; the event is kept.

    Per :meth:`EmbeddingService._commit_embedding`'s sequencing
    rationale: the :class:`TextEmbedded` event commits **before** the
    upsert call runs. An upsert failure leaves a stale event in the
    log; the next :meth:`embed_pending` will see no ``embeddings``
    metadata row and retry the entity, producing a duplicate event
    but eventually a populated vector. The natural-key UNIQUE
    constraint on ``embeddings`` keeps the projection clean.
    """
    embedder = _StubEmbedder()
    _seed_task(migrated_engine, title="vector store boom")

    service = _make_service(
        migrated_engine,
        embedder=embedder,
        vector_store=_FailingVectorStore(),
    )

    with pytest.raises(RuntimeError, match="vector-store upsert failure"):
        service.embed_pending(entity_type="task")
    # The event committed before the upsert was attempted — it is
    # present in the log so the operator can audit the failed attempt.
    embedded_events = _events_of_type(migrated_engine, "embedding.text_embedded")
    assert len(embedded_events) == 1


# ---- error_message sanitisation ------------------------------------------


def test_sanitise_error_redacts_common_token_shapes(migrated_engine: Engine) -> None:
    """sk-* / ghp_* / Bearer * patterns are redacted before persistence."""
    payload = (
        "OpenAI returned 401 for key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345; "
        "GitHub PAT ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 also failed. "
        "Authorization: Bearer abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890"
    )
    embedder = _StubEmbedder(fail_on="trigger", fail_with=RuntimeError(payload))
    _seed_task(migrated_engine, title="trigger")

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="task")
    assert result.failed_count == 1

    failed_events = _events_of_type(migrated_engine, "embedding.failed")
    assert len(failed_events) == 1
    message = failed_events[0].error_message  # type: ignore[attr-defined]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345" not in message
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" not in message
    assert "abc.def.ghi.jkl.mno.pqr.stu.vwx.yz1234567890" not in message
    assert "sk-***" in message
    assert "ghp_***" in message
    assert "Bearer ***" in message


def test_error_message_truncated_to_2000_chars(migrated_engine: Engine) -> None:
    """A 3000-char error → truncated to 2000 (pydantic Field cap)."""
    long_payload = "x" * 3000
    embedder = _StubEmbedder(fail_on="trigger", fail_with=RuntimeError(long_payload))
    _seed_task(migrated_engine, title="trigger")

    service = _make_service(migrated_engine, embedder=embedder)
    service.embed_pending(entity_type="task")

    failed_events = _events_of_type(migrated_engine, "embedding.failed")
    assert len(failed_events) == 1
    event = failed_events[0]
    assert isinstance(event, EmbeddingFailed)
    assert len(event.error_message) == 2000
    assert event.error_message == "x" * 2000


# ---- coverage for inbox / decision branches -------------------------------


def test_embed_pending_covers_inbox_and_decision(migrated_engine: Engine) -> None:
    """Default ``entity_type=None`` walks every supported entity family."""
    embedder = _StubEmbedder()
    task_id = _seed_task(migrated_engine, title="t1")
    decision_id = _seed_decision(migrated_engine, text_value="d1")
    inbox_id = _seed_inbox_item(migrated_engine, summary="i1")
    source_id = _seed_source(migrated_engine, summary="s1", external_id="repo#all")

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending()

    assert result.embedded_count == 4
    embedded = _events_of_type(migrated_engine, "embedding.text_embedded")
    by_type = {e.entity_type: e.entity_id for e in embedded}  # type: ignore[attr-defined]
    assert by_type["task"] == task_id
    assert by_type["decision"] == decision_id
    assert by_type["inbox_item"] == inbox_id
    assert by_type["source"] == source_id


def test_embed_pending_idempotent_second_run_is_noop(migrated_engine: Engine) -> None:
    """Re-running on the same backend is a no-op (冪等 contract, DoD §4)."""
    embedder = _StubEmbedder()
    _seed_task(migrated_engine, title="t1")
    _seed_task(migrated_engine, title="t2")

    service = _make_service(migrated_engine, embedder=embedder)
    first = service.embed_pending(entity_type="task")
    assert first.embedded_count == 2

    # Reset the recorded calls so we can assert the embedder wasn't
    # re-invoked.
    embedder.calls = []
    second = service.embed_pending(entity_type="task")
    assert second.embedded_count == 0
    assert second.skipped_count == 0
    assert embedder.calls == []


# ---- Phase 10 body-based embedding (ADR-0012 改訂版 §4) -------------------


def test_source_embed_prefers_body_over_summary(migrated_engine: Engine) -> None:
    """When a source has both ``body`` and ``summary``, body wins.

    Phase 10 step B2 (ADR-0012 改訂版 §4): the ``source`` entry on
    :data:`opshub.services.embedding_service._SOURCES` switched to
    ``COALESCE(body, summary)``. Verify the embedder receives the
    full body text — not the shorter summary — when both are
    populated.
    """
    embedder = _StubEmbedder()
    _seed_source(
        migrated_engine,
        summary="short summary",
        body="the full body text retained per ADR-0020",
        external_id="repo#with-body",
    )

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="source")

    assert result.embedded_count == 1
    # The embedder saw the body, not the summary.
    assert embedder.calls == ["the full body text retained per ADR-0020"]


def test_source_embed_falls_back_to_summary_when_body_null(
    migrated_engine: Engine,
) -> None:
    """A source with ``body=NULL`` falls back to ``summary`` (backward-compat).

    Phase 3-9 rows and the ``box_drive`` connector (ADR-0019
    §不変条件 (b)) always land with ``body = NULL``. ADR-0012
    改訂版 §4 + ADR-0020 §(d) require the embed path to fall
    through to ``summary`` so historic data keeps producing
    vectors.
    """
    embedder = _StubEmbedder()
    _seed_source(
        migrated_engine,
        summary="summary-only legacy row",
        body=None,
        external_id="repo#legacy",
    )

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="source")

    assert result.embedded_count == 1
    assert embedder.calls == ["summary-only legacy row"]


def test_source_with_both_body_and_summary_null_is_skipped(
    migrated_engine: Engine,
) -> None:
    """No body and no summary → skipped (no useful vector)."""
    embedder = _StubEmbedder()
    _seed_source(
        migrated_engine,
        summary=None,
        body=None,
        external_id="repo#blank",
    )

    service = _make_service(migrated_engine, embedder=embedder)
    result = service.embed_pending(entity_type="source")

    assert result.embedded_count == 0
    assert result.skipped_count == 1
    assert embedder.calls == []


def test_source_embed_one_if_pending_uses_body_fallback(
    migrated_engine: Engine,
) -> None:
    """The single-row hook path honours the same body fallback.

    :meth:`EmbeddingService.embed_one_if_pending` is the Phase 5
    step C1 auto-embed hook entry. It reads through
    :meth:`_fetch_pending_text`, which must apply the same
    ``COALESCE(body, summary)`` shape as :meth:`embed_pending`.
    """
    embedder = _StubEmbedder()
    source_id = _seed_source(
        migrated_engine,
        summary="short",
        body="full body via hook",
        external_id="repo#hook",
    )

    service = _make_service(migrated_engine, embedder=embedder)
    embedded = service.embed_one_if_pending("source", source_id)

    assert embedded is True
    assert embedder.calls == ["full body via hook"]


# ---- purge_embeddings (Phase 10 step B2) ---------------------------------


def test_purge_embeddings_clears_scoped_metadata_rows(
    migrated_engine: Engine,
) -> None:
    """``purge_embeddings`` drops existing rows for the scope.

    Phase 10 step B2: when the embed-input shape changes
    (summary → body) but ``model_id`` / ``model_version`` stay the
    same, operators run ``opshub embeddings rebuild --purge`` to
    force re-embed. The purge step must clear the affected entity
    family — verified via the metadata-table row count.
    """
    embedder = _StubEmbedder()
    _seed_source(
        migrated_engine,
        summary="initial",
        body=None,
        external_id="repo#purge",
    )

    service = _make_service(migrated_engine, embedder=embedder)
    first = service.embed_pending(entity_type="source")
    assert first.embedded_count == 1

    purged = service.purge_embeddings(entity_type="source")
    assert purged == 1

    # The next rebuild re-embeds the row (the NOT EXISTS predicate
    # now sees zero rows for the active model).
    embedder.calls = []
    second = service.embed_pending(entity_type="source")
    assert second.embedded_count == 1
    assert embedder.calls == ["initial"]


def test_purge_embeddings_no_scope_clears_every_entity_family(
    migrated_engine: Engine,
) -> None:
    """``entity_type=None`` purges every supported entity family."""
    embedder = _StubEmbedder()
    _seed_task(migrated_engine, title="t1")
    _seed_source(
        migrated_engine,
        summary="s1",
        body=None,
        external_id="repo#all",
    )

    service = _make_service(migrated_engine, embedder=embedder)
    first = service.embed_pending()
    assert first.embedded_count == 2

    purged = service.purge_embeddings()
    assert purged == 2


def test_purge_embeddings_no_op_when_nothing_embedded(
    migrated_engine: Engine,
) -> None:
    """An empty purge is a zero-cost no-op (returns 0)."""
    embedder = _StubEmbedder()
    service = _make_service(migrated_engine, embedder=embedder)
    assert service.purge_embeddings(entity_type="source") == 0
