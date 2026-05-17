"""Tests for :class:`opshub.services.recall_service.RecallService`.

The unit suite drives the service through a migrated SQLite engine
(the same ``migrated_engine`` fixture pattern used by
:mod:`tests.unit.services.test_embedding_service`) because the service
reads the per-entity projection tables for metadata attachment AND the
``embeddings`` metadata table for the active-model existence check on
every recall call.

Stubs
-----

* :class:`_StubEmbedder` — returns a deterministic vector so the test
  never depends on a real model. The embed call is recorded so we
  can assert the embedder was (or was not) invoked.
* :class:`_StubVectorStore` — its :meth:`recall` returns a
  predetermined list of :class:`opshub.vectors.store.RecallHit`
  instances. Each call records the ``(k, entity_types)`` it was
  passed so tests can verify the pass-through behaviour.

Existence-check seeding
-----------------------

The service's :meth:`RecallService._assert_embeddings_exist_for_active_model`
queries the ``embeddings`` metadata table. Most tests seed exactly
one row via :func:`_seed_embedding_metadata_row` so the existence
check passes and the recall pipeline runs; the
``raises_config_error`` variant deliberately leaves the table empty.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError
from opshub.core.ids import new_ulid
from opshub.core.time import now_utc
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table
from opshub.services.recall_service import RecallService
from opshub.vectors.embedder import EmbeddingResult
from opshub.vectors.store import RecallHit as VectorRecallHit
from opshub.vectors.store import StoredEmbedding

if TYPE_CHECKING:
    from opshub.vectors.embedder import Embedder
    from opshub.vectors.store import VectorStore


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

    Mirrors :mod:`tests.unit.services.test_embedding_service` —
    sqlite-vec is required because migration 0013 (PR #64) creates
    the ``embeddings_vec_*`` virtual tables.
    """
    db_path = tmp_path / "recall_service.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


class _StubEmbedder:
    """Embedder stub that returns a deterministic vector.

    The vector value is irrelevant to the recall tests — the
    :class:`_StubVectorStore` returns a pre-baked hit list regardless
    of the query vector — but the embedder identity (``model_id`` /
    ``model_version``) IS used by
    :meth:`RecallService._assert_embeddings_exist_for_active_model`.
    """

    def __init__(
        self,
        *,
        model_id: str = "stub-embedder",
        model_version: str = "v1",
        dim: int = 4,
    ) -> None:
        self._model_id = model_id
        self._model_version = model_version
        self._dim = dim
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
        return EmbeddingResult(
            vector=tuple(0.1 * i for i in range(self._dim)),
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


class _StubVectorStore:
    """VectorStore stub that returns a pre-baked recall list.

    Records every ``recall`` invocation so tests can assert that the
    service passes through ``k`` + ``entity_types`` correctly.
    """

    def __init__(self, hits: list[VectorRecallHit]) -> None:
        self._hits = hits
        self.recall_calls: list[tuple[int, list[str] | None]] = []

    def upsert(self, embeddings: list[StoredEmbedding]) -> None:  # pragma: no cover
        del embeddings

    def recall(
        self,
        query: tuple[float, ...],
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[VectorRecallHit]:
        del query
        self.recall_calls.append((k, entity_types))
        return list(self._hits)

    def count(self, *, entity_type: str | None = None) -> int:  # pragma: no cover
        del entity_type
        return 0

    def delete(self, *, entity_type: str, entity_id: str) -> int:  # pragma: no cover
        del entity_type, entity_id
        return 0


def _vec_hit(entity_type: str, entity_id: str, score: float, dim: int = 4) -> VectorRecallHit:
    """Build a :class:`opshub.vectors.store.RecallHit` with a dummy vector.

    The vector value is irrelevant to the recall tests — only
    ``(entity_type, entity_id, score)`` are consulted downstream.
    """
    return VectorRecallHit(
        entity_type=entity_type,
        entity_id=entity_id,
        score=score,
        vector=tuple(0.0 for _ in range(dim)),
    )


def _seed_task(engine: Engine, *, title: str, state: str = "active") -> str:
    """Insert one :data:`tasks_table` row with the given ``state``."""
    task_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(tasks_table).values(
                id=task_id,
                title=title,
                body=None,
                state=state,
                result_note=None,
                created_at=now,
                updated_at=now,
            )
        )
    return task_id


def _seed_decision(engine: Engine, *, text_value: str) -> str:
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


def _seed_inbox_item(engine: Engine, *, summary: str, state: str = "pending") -> str:
    item_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(inbox_items_table).values(
                id=item_id,
                summary=summary,
                source_ref=None,
                state=state,
                disposition=None,
                target_id=None,
                reason=None,
                created_at=now,
                updated_at=now,
            )
        )
    return item_id


def _seed_source(engine: Engine, *, title: str, external_id: str) -> str:
    source_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name="github",
                external_id=external_id,
                source_type="issue",
                title=title,
                url=None,
                summary="ignored summary",
                observed_at=now,
                updated_at=now,
            )
        )
    return source_id


def _seed_embedding_metadata_row(
    engine: Engine,
    *,
    embedder: Embedder,
    entity_type: str = "task",
    entity_id: str | None = None,
) -> None:
    """Write one row into ``embeddings`` so the existence check passes.

    The active-model existence check is dim-agnostic — it filters on
    ``(model_id, model_version)`` only — so any single row with the
    embedder's identity is sufficient.
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
                "eid": entity_id or new_ulid(),
                "mid": embedder.model_id,
                "mv": embedder.model_version,
                "dim": embedder.dim or 4,
                "ts": now_utc(),
            },
        )


def _make_service(
    engine: Engine,
    *,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
) -> RecallService:
    """Build a :class:`RecallService` against the migrated engine.

    The default :class:`_StubVectorStore` returns an empty hit list;
    most tests override it with a pre-baked list.
    """
    return RecallService(
        embedder=embedder if embedder is not None else _StubEmbedder(),
        vector_store=vector_store if vector_store is not None else _StubVectorStore([]),
        engine=engine,
    )


# ---- validation -----------------------------------------------------------


def test_recall_empty_query_raises_config_error(migrated_engine: Engine) -> None:
    """An empty / whitespace-only query is rejected loudly."""
    service = _make_service(migrated_engine)
    with pytest.raises(ConfigError, match="must not be empty"):
        service.recall("")
    with pytest.raises(ConfigError, match="must not be empty"):
        service.recall("   ")


def test_recall_no_embeddings_for_model_raises_config_error_with_rebuild_hint(
    migrated_engine: Engine,
) -> None:
    """Zero matching ``embeddings`` rows → ConfigError mentioning rebuild."""
    embedder = _StubEmbedder()
    # NOTE: deliberately do NOT seed an embeddings row.
    service = _make_service(migrated_engine, embedder=embedder)
    with pytest.raises(ConfigError, match="opshub embeddings rebuild"):
        service.recall("anything")
    # The embedder must not have been called — the check runs first.
    assert embedder.calls == []


def test_recall_state_filter_with_decision_raises_config_error(
    migrated_engine: Engine,
) -> None:
    """``--state`` + ``entity_type=decision`` is a caller mistake."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    service = _make_service(migrated_engine, embedder=embedder)
    with pytest.raises(ConfigError, match="no state column"):
        service.recall("query", entity_type="decision", state="anything")


def test_recall_state_filter_with_source_raises_config_error(
    migrated_engine: Engine,
) -> None:
    """Same constraint for ``entity_type=source`` (no state column)."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    service = _make_service(migrated_engine, embedder=embedder)
    with pytest.raises(ConfigError, match="no state column"):
        service.recall("query", entity_type="source", state="anything")


# ---- happy paths ----------------------------------------------------------


def test_recall_returns_hits_in_score_order(migrated_engine: Engine) -> None:
    """Vector store order is preserved in the returned RecallHit list."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    task_a = _seed_task(migrated_engine, title="alpha")
    task_b = _seed_task(migrated_engine, title="bravo")
    task_c = _seed_task(migrated_engine, title="charlie")
    store = _StubVectorStore(
        [
            _vec_hit("task", task_a, score=0.95),
            _vec_hit("task", task_b, score=0.80),
            _vec_hit("task", task_c, score=0.65),
        ]
    )
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    hits = service.recall("any query")

    assert [h.entity_id for h in hits] == [task_a, task_b, task_c]
    assert [h.title for h in hits] == ["alpha", "bravo", "charlie"]
    assert [h.score for h in hits] == [0.95, 0.80, 0.65]
    # All embedded as tasks.
    assert all(h.entity_type == "task" for h in hits)


def test_recall_skips_orphan_embeddings_when_entity_deleted(
    migrated_engine: Engine,
) -> None:
    """A vector hit with no matching projection row is silently dropped."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    real_task = _seed_task(migrated_engine, title="real")
    store = _StubVectorStore(
        [
            _vec_hit("task", "missing-task-id", score=0.99),
            _vec_hit("task", real_task, score=0.50),
        ]
    )
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    hits = service.recall("query")

    # Only the real task survives the orphan filter.
    assert len(hits) == 1
    assert hits[0].entity_id == real_task
    assert hits[0].title == "real"


def test_recall_entity_type_filter_passes_through_to_vector_store(
    migrated_engine: Engine,
) -> None:
    """``entity_type='task'`` reaches :meth:`VectorStore.recall`."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    store = _StubVectorStore([])
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    service.recall("query", entity_type="task")

    assert len(store.recall_calls) == 1
    _k, entity_types = store.recall_calls[0]
    assert entity_types == ["task"]


def test_recall_state_filter_excludes_non_matching(migrated_engine: Engine) -> None:
    """Seeded ``active`` + ``completed`` tasks, ``state='active'`` → active only."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    active_a = _seed_task(migrated_engine, title="active-a", state="active")
    completed = _seed_task(migrated_engine, title="completed", state="completed")
    active_b = _seed_task(migrated_engine, title="active-b", state="active")
    store = _StubVectorStore(
        [
            _vec_hit("task", active_a, score=0.99),
            _vec_hit("task", completed, score=0.80),
            _vec_hit("task", active_b, score=0.60),
        ]
    )
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    hits = service.recall("query", state="active")

    assert [h.entity_id for h in hits] == [active_a, active_b]


def test_recall_attaches_title_from_each_entity_type(migrated_engine: Engine) -> None:
    """Each entity_type's display column maps to ``RecallHit.title``."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    task_id = _seed_task(migrated_engine, title="task title")
    decision_id = _seed_decision(migrated_engine, text_value="decision text")
    inbox_id = _seed_inbox_item(migrated_engine, summary="inbox summary")
    source_id = _seed_source(migrated_engine, title="source title", external_id="repo#1")
    store = _StubVectorStore(
        [
            _vec_hit("task", task_id, score=0.9),
            _vec_hit("decision", decision_id, score=0.8),
            _vec_hit("inbox_item", inbox_id, score=0.7),
            _vec_hit("source", source_id, score=0.6),
        ]
    )
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    hits = service.recall("query")

    titles_by_type = {h.entity_type: h.title for h in hits}
    assert titles_by_type == {
        "task": "task title",
        "decision": "decision text",
        "inbox_item": "inbox summary",
        "source": "source title",
    }


def test_recall_limit_applies_after_state_filter(migrated_engine: Engine) -> None:
    """``limit=2`` + state filter returns the top-2 STATE-matching hits.

    Five tasks in score-descending order: active, completed, active,
    completed, active. The top-2 overall would include a completed
    task; the service must drop completed tasks first and then cap
    the result at 2 active ones.
    """
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    a1 = _seed_task(migrated_engine, title="a1", state="active")
    c1 = _seed_task(migrated_engine, title="c1", state="completed")
    a2 = _seed_task(migrated_engine, title="a2", state="active")
    c2 = _seed_task(migrated_engine, title="c2", state="completed")
    a3 = _seed_task(migrated_engine, title="a3", state="active")
    store = _StubVectorStore(
        [
            _vec_hit("task", a1, score=0.99),
            _vec_hit("task", c1, score=0.90),
            _vec_hit("task", a2, score=0.80),
            _vec_hit("task", c2, score=0.70),
            _vec_hit("task", a3, score=0.60),
        ]
    )
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    hits = service.recall("query", state="active", limit=2)

    assert [h.entity_id for h in hits] == [a1, a2]


def test_recall_default_limit_caps_at_ten(migrated_engine: Engine) -> None:
    """Default ``limit=10`` collects no more than ten hits."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    seeded = [_seed_task(migrated_engine, title=f"t{i}") for i in range(12)]
    store = _StubVectorStore(
        [_vec_hit("task", tid, score=1.0 - 0.05 * idx) for idx, tid in enumerate(seeded)]
    )
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    hits = service.recall("query")

    assert len(hits) == 10


def test_recall_vector_store_called_with_2x_limit(migrated_engine: Engine) -> None:
    """Service requests ``2*limit`` from the vector store for filter headroom."""
    embedder = _StubEmbedder()
    _seed_embedding_metadata_row(migrated_engine, embedder=embedder)
    store = _StubVectorStore([])
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)

    service.recall("query", limit=5)

    assert store.recall_calls == [(10, None)]
