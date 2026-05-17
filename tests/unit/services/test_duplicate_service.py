"""Tests for :class:`opshub.services.duplicate_service.DuplicateService`.

Approach mirrors :mod:`tests.unit.services.test_embedding_service`:
drive the service through a real migrated SQLite engine so the
``embeddings`` metadata JOIN exercises the SQL path, and stub the
:class:`Embedder` + :class:`VectorStore` so the test never depends
on a model file or sqlite-vec MATCH semantics.

The stubs are deliberately small:

* :class:`_StubEmbedder` exposes the ``model_id`` / ``model_version``
  the service joins on. ``embed_one`` is not invoked by the
  duplicate scan after the Phase 4 follow-up (lookup goes through
  :meth:`VectorStore.recall_by_rowid`), so the stub records calls
  and the tests assert the recorded list stays empty.
* :class:`_ScriptedVectorStore` lets each test enumerate the
  ``RecallHit``s it wants per ``(entity_id, k)`` lookup, modelling a
  cosine-similarity-by-fiat fixture.

The conversion fixture (``test_distance_to_cosine_similarity_*``)
exercises :func:`_score_to_cosine_similarity` directly to pin the
boundary values; the integration coverage comes from the
``test_find_duplicates_*`` cases.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError
from opshub.core.ids import new_ulid
from opshub.core.time import now_utc
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.sources import sources_table
from opshub.services.duplicate_service import (
    DuplicatePair,
    DuplicateService,
    _score_to_cosine_similarity,  # pyright: ignore[reportPrivateUsage]
)
from opshub.vectors.embedder import EmbeddingResult
from opshub.vectors.store import RecallHit, StoredEmbedding

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


# ---- fixtures + stubs -----------------------------------------------------


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh migrated SQLite DB.

    Same shape as :func:`tests.unit.services.test_embedding_service.migrated_engine`
    — runs ``alembic upgrade head`` (which includes migration 0013
    requiring the ``[vector]`` extras).
    """
    db_path = tmp_path / "duplicate_service.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


class _StubEmbedder:
    """Deterministic embedder.

    The actual vector is unused by these tests (the scripted
    :class:`VectorStore` ignores it) but the ``model_id`` +
    ``model_version`` must match the row written to the
    ``embeddings`` projection so the service's EXISTS JOIN finds the
    entity.
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
            vector=tuple(0.0 for _ in range(self._dim)),
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


class _ScriptedVectorStore:
    """VectorStore that replays canned recall hits keyed on call ordering.

    Tests pass a list-of-lists where each inner list is the result of
    one :meth:`recall_by_rowid` call (in the order the service invokes
    it). Call-ordering matches the iteration order of
    :meth:`DuplicateService._iter_embedded_entities`, which itself
    follows the ``embeddings``-EXISTS filter against the projection
    table (insertion order on SQLite).
    """

    def __init__(
        self,
        scripted_hits: list[list[RecallHit]] | None = None,
    ) -> None:
        self._scripted: list[list[RecallHit]] = list(scripted_hits or [])
        self.recall_by_rowid_calls: list[tuple[str, str, int, list[str] | None]] = []

    def upsert(self, embeddings: list[StoredEmbedding]) -> None:  # pragma: no cover
        del embeddings

    def recall(
        self,
        query: tuple[float, ...],
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:  # pragma: no cover
        del query, k, entity_types
        # The service uses :meth:`recall_by_rowid` exclusively; this
        # method is here only so the stub still satisfies the
        # ``runtime_checkable`` :class:`VectorStore` Protocol.
        return []

    def recall_by_rowid(
        self,
        entity_type: str,
        entity_id: str,
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:
        self.recall_by_rowid_calls.append((entity_type, entity_id, k, entity_types))
        if not self._scripted:
            return []
        return self._scripted.pop(0)

    def count(self, *, entity_type: str | None = None) -> int:  # pragma: no cover
        del entity_type
        return 0

    def delete(self, *, entity_type: str, entity_id: str) -> int:  # pragma: no cover
        del entity_type, entity_id
        return 0


def _similarity_to_score(similarity: float) -> float:
    """Inverse of :func:`_score_to_cosine_similarity` for fixture authoring.

    Given a target cosine similarity, return the ``score`` value that
    :func:`opshub.services.duplicate_service._score_to_cosine_similarity`
    would convert back to ``similarity``. Used by the scripted
    :class:`VectorStore` so tests can express "I want this neighbour
    to look like 0.95 similarity" directly.

    ``cosine_similarity = 1 - score^2 / 2``  ⇒
    ``score = -sqrt(2 * (1 - similarity))``. We pick the negative
    branch to match the sqlite-vec backend's ``score = -L2_distance``
    sign convention; either branch round-trips because the formula
    squares the score.
    """
    return -math.sqrt(max(0.0, 2.0 * (1.0 - similarity)))


def _seed_source_with_embedding(
    engine: Engine,
    *,
    summary: str | None,
    external_id: str,
    model_id: str,
    model_version: str,
) -> str:
    """Insert one source row + one matching ``embeddings`` metadata row.

    The duplicate service joins on the ``embeddings`` projection to
    decide which entities are in scope, so the test fixture must
    write both halves — otherwise the entity is invisible to the
    scan.
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
            )
        )
        if summary is not None:
            # Real production wouldn't write the embeddings row for an
            # empty summary either (the embedding service skips them).
            # We mirror that here so the "skip without text" case
            # matches reality.
            conn.execute(
                text(
                    "INSERT INTO embeddings "
                    "(entity_type, entity_id, model_id, model_version, "
                    " dim, created_at) VALUES "
                    "(:et, :eid, :mid, :mv, :dim, :ts)"
                ),
                {
                    "et": "source",
                    "eid": source_id,
                    "mid": model_id,
                    "mv": model_version,
                    "dim": 4,
                    "ts": now,
                },
            )
    return source_id


def _make_service(
    engine: Engine,
    *,
    embedder: _StubEmbedder | None = None,
    vector_store: _ScriptedVectorStore | None = None,
) -> DuplicateService:
    return DuplicateService(
        embedder=embedder if embedder is not None else _StubEmbedder(),
        vector_store=vector_store if vector_store is not None else _ScriptedVectorStore(),
        engine=engine,
    )


# ---- find_duplicates ------------------------------------------------------


def test_find_duplicates_returns_pairs_above_threshold(
    migrated_engine: Engine,
) -> None:
    """Hits at {0.95, 0.85, 0.50} with threshold=0.90 → one pair."""
    embedder = _StubEmbedder()
    a_id = _seed_source_with_embedding(
        migrated_engine,
        summary="alpha",
        external_id="repo#a",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )
    b_id = _seed_source_with_embedding(
        migrated_engine,
        summary="beta",
        external_id="repo#b",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )
    c_id = _seed_source_with_embedding(
        migrated_engine,
        summary="gamma",
        external_id="repo#c",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )
    d_id = _seed_source_with_embedding(
        migrated_engine,
        summary="delta",
        external_id="repo#d",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )

    # Source A's neighbours: B at 0.95 (duplicate), C at 0.85 (below),
    # D at 0.50 (below). Subsequent sources B/C/D have no above-threshold
    # neighbours.
    store = _ScriptedVectorStore(
        scripted_hits=[
            [
                RecallHit(
                    entity_type="source",
                    entity_id=a_id,  # self (skipped)
                    score=_similarity_to_score(1.0),
                    vector=(0.0,),
                ),
                RecallHit(
                    entity_type="source",
                    entity_id=b_id,
                    score=_similarity_to_score(0.95),
                    vector=(0.0,),
                ),
                RecallHit(
                    entity_type="source",
                    entity_id=c_id,
                    score=_similarity_to_score(0.85),
                    vector=(0.0,),
                ),
                RecallHit(
                    entity_type="source",
                    entity_id=d_id,
                    score=_similarity_to_score(0.50),
                    vector=(0.0,),
                ),
            ],
            [],  # B's neighbours
            [],  # C's neighbours
            [],  # D's neighbours
        ]
    )

    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)
    pairs = service.find_duplicates(threshold=0.90)

    assert len(pairs) == 1
    pair = pairs[0]
    assert {pair.entity_id_a, pair.entity_id_b} == {a_id, b_id}
    assert pair.entity_type == "source"
    assert math.isclose(pair.similarity, 0.95, abs_tol=1e-6)
    # Phase 4 follow-up contract: lookup goes through recall_by_rowid;
    # the embedder is never asked to re-embed source text on a scan.
    assert embedder.calls == []
    assert [call[:2] for call in store.recall_by_rowid_calls] == [
        ("source", a_id),
        ("source", b_id),
        ("source", c_id),
        ("source", d_id),
    ]


def test_find_duplicates_deduplicates_reverse_pairs(
    migrated_engine: Engine,
) -> None:
    """A finds B and B finds A → only one DuplicatePair returned."""
    embedder = _StubEmbedder()
    a_id = _seed_source_with_embedding(
        migrated_engine,
        summary="alpha",
        external_id="repo#a",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )
    b_id = _seed_source_with_embedding(
        migrated_engine,
        summary="beta",
        external_id="repo#b",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )

    store = _ScriptedVectorStore(
        scripted_hits=[
            [
                RecallHit(
                    entity_type="source",
                    entity_id=b_id,
                    score=_similarity_to_score(0.97),
                    vector=(0.0,),
                ),
            ],
            [
                RecallHit(
                    entity_type="source",
                    entity_id=a_id,
                    score=_similarity_to_score(0.97),
                    vector=(0.0,),
                ),
            ],
        ]
    )

    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)
    pairs = service.find_duplicates(threshold=0.90)

    assert len(pairs) == 1
    pair = pairs[0]
    # ordered ascending
    assert pair.entity_id_a < pair.entity_id_b
    assert {pair.entity_id_a, pair.entity_id_b} == {a_id, b_id}


def test_find_duplicates_excludes_self_match(migrated_engine: Engine) -> None:
    """Source returning itself as a hit → no pair emitted for that hit."""
    embedder = _StubEmbedder()
    a_id = _seed_source_with_embedding(
        migrated_engine,
        summary="alpha",
        external_id="repo#a",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )

    store = _ScriptedVectorStore(
        scripted_hits=[
            [
                RecallHit(
                    entity_type="source",
                    entity_id=a_id,
                    score=_similarity_to_score(1.0),
                    vector=(0.0,),
                ),
            ],
        ]
    )

    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)
    pairs = service.find_duplicates(threshold=0.50)

    assert pairs == []


def test_find_duplicates_respects_limit(migrated_engine: Engine) -> None:
    """5 above-threshold pairs with limit=2 → 2 highest-similarity pairs returned."""
    embedder = _StubEmbedder()
    # Six sources: A pairs with B/C/D/E/F at varying similarities.
    ids = [
        _seed_source_with_embedding(
            migrated_engine,
            summary=f"summary {i}",
            external_id=f"repo#{i}",
            model_id=embedder.model_id,
            model_version=embedder.model_version,
        )
        for i in range(6)
    ]
    a_id, *rest_ids = ids

    similarities = [0.99, 0.98, 0.97, 0.96, 0.95]
    store = _ScriptedVectorStore(
        scripted_hits=[
            # A finds five above-threshold neighbours
            [
                RecallHit(
                    entity_type="source",
                    entity_id=other_id,
                    score=_similarity_to_score(sim),
                    vector=(0.0,),
                )
                for other_id, sim in zip(rest_ids, similarities, strict=True)
            ],
            # Remaining iterations: no further hits (already de-duped)
            *[
                [
                    RecallHit(
                        entity_type="source",
                        entity_id=a_id,
                        score=_similarity_to_score(sim),
                        vector=(0.0,),
                    ),
                ]
                for sim in similarities
            ],
        ]
    )

    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)
    pairs = service.find_duplicates(threshold=0.90, limit=2)

    assert len(pairs) == 2
    # Top two = 0.99 + 0.98
    assert math.isclose(pairs[0].similarity, 0.99, abs_tol=1e-6)
    assert math.isclose(pairs[1].similarity, 0.98, abs_tol=1e-6)


def test_find_duplicates_sorts_by_similarity_descending(
    migrated_engine: Engine,
) -> None:
    """Returned list is sorted highest-similarity first."""
    embedder = _StubEmbedder()
    ids = [
        _seed_source_with_embedding(
            migrated_engine,
            summary=f"summary {i}",
            external_id=f"repo#{i}",
            model_id=embedder.model_id,
            model_version=embedder.model_version,
        )
        for i in range(4)
    ]
    _a_id, *rest = ids

    # Author hits out of order: 0.91, 0.99, 0.95.
    store = _ScriptedVectorStore(
        scripted_hits=[
            [
                RecallHit(
                    entity_type="source",
                    entity_id=rest[0],
                    score=_similarity_to_score(0.91),
                    vector=(0.0,),
                ),
                RecallHit(
                    entity_type="source",
                    entity_id=rest[1],
                    score=_similarity_to_score(0.99),
                    vector=(0.0,),
                ),
                RecallHit(
                    entity_type="source",
                    entity_id=rest[2],
                    score=_similarity_to_score(0.95),
                    vector=(0.0,),
                ),
            ],
            [],
            [],
            [],
        ]
    )

    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)
    pairs = service.find_duplicates(threshold=0.90)

    assert [round(p.similarity, 2) for p in pairs] == [0.99, 0.95, 0.91]


@pytest.mark.parametrize("bad_threshold", [-0.1, 1.5, -1e-9, 1.0 + 1e-9])
def test_find_duplicates_threshold_out_of_range_raises_config_error(
    migrated_engine: Engine, bad_threshold: float
) -> None:
    """Threshold outside ``[0, 1]`` → :class:`ConfigError`."""
    service = _make_service(migrated_engine)
    with pytest.raises(ConfigError, match="threshold must be in"):
        service.find_duplicates(threshold=bad_threshold)


def test_find_duplicates_unknown_entity_type_raises_config_error(
    migrated_engine: Engine,
) -> None:
    """Unknown ``entity_type`` → :class:`ConfigError` listing valid options."""
    service = _make_service(migrated_engine)
    with pytest.raises(ConfigError, match="unknown entity_type"):
        service.find_duplicates(entity_type="invalid")


def test_find_duplicates_skips_entities_without_text(
    migrated_engine: Engine,
) -> None:
    """A seeded source with ``summary=None`` is invisible to the scan.

    The embedding service skips empty text and never writes a metadata
    row; the fixture mirrors that production path. The duplicate
    service's EXISTS JOIN therefore excludes the source, so the
    embedder is never called for it.
    """
    embedder = _StubEmbedder()
    _seed_source_with_embedding(
        migrated_engine,
        summary=None,
        external_id="repo#empty",
        model_id=embedder.model_id,
        model_version=embedder.model_version,
    )

    store = _ScriptedVectorStore()
    service = _make_service(migrated_engine, embedder=embedder, vector_store=store)
    pairs = service.find_duplicates(threshold=0.90)

    assert pairs == []
    # Embedder is never invoked at all on a duplicate scan (lookup
    # uses :meth:`VectorStore.recall_by_rowid`); double-checking the
    # empty-summary source was also excluded by the EXISTS JOIN so no
    # recall happened either.
    assert embedder.calls == []
    assert store.recall_by_rowid_calls == []


# ---- _score_to_cosine_similarity ------------------------------------------


def test_distance_to_cosine_similarity_handles_floating_point_edge_cases() -> None:
    """Boundary inputs for the L2 → cosine similarity conversion.

    For unit-normalised vectors:
        * L2=0   ⇒ cosine_similarity = 1
        * L2=√2  ⇒ cosine_similarity = 0 (maximally distant)
        * L2>√2  ⇒ clamps to 0 (floating-point noise / non-normalised
          query)

    The function's input variable is named ``score`` because the
    :class:`SqliteVecStore` negates the raw distance — but squaring
    is sign-stable, so both ``+L2`` and ``-L2`` round-trip to the
    same similarity.
    """
    assert _score_to_cosine_similarity(0.0) == 1.0
    assert math.isclose(_score_to_cosine_similarity(math.sqrt(2.0)), 0.0, abs_tol=1e-9)
    assert math.isclose(_score_to_cosine_similarity(-math.sqrt(2.0)), 0.0, abs_tol=1e-9)
    # Beyond the unit-normalised limit: clamped to 0
    assert _score_to_cosine_similarity(2.0) == 0.0
    assert _score_to_cosine_similarity(-2.0) == 0.0
    # Mid-range
    assert _score_to_cosine_similarity(1.0) == 0.5


def test_duplicate_pair_is_immutable() -> None:
    """:class:`DuplicatePair` is frozen so callers cannot mutate it post-hoc.

    Documents the dataclass contract — the slot/frozen pair is what
    lets the CLI safely cache pairs without defensive copies.
    """
    pair = DuplicatePair(
        entity_type="source",
        entity_id_a="01AAAA",
        entity_id_b="01BBBB",
        text_a="alpha",
        text_b="beta",
        similarity=0.95,
    )
    with pytest.raises((AttributeError, TypeError)):
        pair.similarity = 0.5  # type: ignore[misc]
