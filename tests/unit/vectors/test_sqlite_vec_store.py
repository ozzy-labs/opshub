"""Tests for :class:`SqliteVecStore` (Phase 4 step A2).

These exercises require the ``[vector]`` extras (sqlite-vec + numpy)
because migration 0013 emits ``CREATE VIRTUAL TABLE ... USING vec0``
on alembic upgrade and the store itself round-trips real vectors
through that virtual table. The whole module is skipped via
:func:`pytest.importorskip` when ``sqlite_vec`` is not importable so
contributors who ``uv sync --extra dev`` (no vector extras) don't trip
the alembic upgrade with ``no such module: vec0``.

Each test owns its own tmp-scoped SQLite file (provisioned via
``alembic upgrade head``) so individual tests can run in any order and
in parallel under ``pytest -n auto``.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip(
    "sqlite_vec",
    reason="opshub.vectors.sqlite_vec_store tests require the 'vector' extras",
)

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from opshub.core.errors import ConfigError
from opshub.db.engine import create_engine_for_sqlite
from opshub.vectors.sqlite_vec_store import (
    VEC_TABLES_BY_DIM,
    SqliteVecStore,
    _blob_to_vec,  # pyright: ignore[reportPrivateUsage]
    _vec_to_blob,  # pyright: ignore[reportPrivateUsage]
)
from opshub.vectors.store import StoredEmbedding, VectorStore

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


# ---- fixtures + helpers ---------------------------------------------------


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to ``db_path``.

    Mirrors the helper in ``tests/integration/test_phase4_migrations.py``
    so we hit the same env.py URL-resolution path the production CLI
    uses (including the sqlite-vec extension load on the migration
    connection).
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head`` and bind an engine.

    The engine factory wires the sqlite-vec extension loader, so the
    vec0 virtual tables created by migration 0013 are queryable
    immediately on every pooled connection.
    """
    db_path = tmp_path / "store.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    eng = create_engine_for_sqlite(db_path)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def store(engine: Engine) -> SqliteVecStore:
    """Default-constructed :class:`SqliteVecStore` bound to the test engine."""
    return SqliteVecStore(engine)


def _make_vector(dim: int, *, fill: float = 0.0) -> tuple[float, ...]:
    """Return a ``dim``-length tuple filled with a constant float.

    Constant fills make distance ordering predictable in the recall
    tests below — pairwise distances collapse to a function of the
    fill value, which removes any dependence on a real model's
    geometry.
    """
    return tuple(fill for _ in range(dim))


def _ts() -> datetime:
    """Stable tz-aware timestamp for fixtures (created_at requirement)."""
    return datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)


# ---- Protocol conformance -------------------------------------------------


def test_satisfies_vector_store_protocol(store: SqliteVecStore) -> None:
    """The class must structurally satisfy the frozen Phase 1 Protocol.

    ``VectorStore`` is ``@runtime_checkable`` so an ``isinstance``
    check is the strongest single assertion we can make about the
    method surface without enumerating each name.
    """
    assert isinstance(store, VectorStore)


# ---- upsert ---------------------------------------------------------------


def test_upsert_empty_list_is_noop(store: SqliteVecStore, engine: Engine) -> None:
    """Empty input must not open a transaction or write anything.

    Mirrors the Phase 1 ``Embedder.embed([])`` semantics — fan-out
    callers may pass an empty list when they have nothing to embed
    and we must not surface that as a write.
    """
    store.upsert([])
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM embeddings")).scalar_one()
    assert count == 0


def test_upsert_inserts_metadata_and_vector_row(
    store: SqliteVecStore,
    engine: Engine,
) -> None:
    """One upsert writes one ``embeddings`` row + one vec0 row at the same rowid."""
    emb = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="bge-m3",
        model_version="v1",
        vector=_make_vector(1024, fill=0.1),
        created_at=_ts(),
    )
    store.upsert([emb])

    with engine.connect() as conn:
        meta_rows = conn.execute(
            text("SELECT rowid, entity_type, entity_id, dim FROM embeddings")
        ).all()
        vec_rows = conn.execute(text("SELECT rowid FROM embeddings_vec_local")).all()

    assert len(meta_rows) == 1
    assert meta_rows[0].entity_type == "task"
    assert meta_rows[0].entity_id == "01J0"
    assert meta_rows[0].dim == 1024

    # The vec0 row must share the metadata rowid — that JOIN key is
    # how ``recall`` reconstructs (entity_type, entity_id) from a hit.
    assert len(vec_rows) == 1
    assert vec_rows[0].rowid == meta_rows[0].rowid


def test_upsert_replaces_existing_natural_key(
    store: SqliteVecStore,
    engine: Engine,
) -> None:
    """Upserting twice with the same natural key keeps exactly one row.

    The natural key is
    ``(entity_type, entity_id, model_id, model_version)`` per the
    UNIQUE constraint declared in migration 0002. vec0 has no UPDATE
    so :meth:`SqliteVecStore.upsert` must DELETE-then-INSERT to honour
    the constraint without violating it.
    """
    base = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="bge-m3",
        model_version="v1",
        vector=_make_vector(1024, fill=0.1),
        created_at=_ts(),
    )
    store.upsert([base])

    updated = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="bge-m3",
        model_version="v1",
        vector=_make_vector(1024, fill=0.5),
        created_at=_ts(),
    )
    store.upsert([updated])

    with engine.connect() as conn:
        meta_count = conn.execute(text("SELECT COUNT(*) FROM embeddings")).scalar_one()
        vec_count = conn.execute(text("SELECT COUNT(*) FROM embeddings_vec_local")).scalar_one()
        stored = conn.execute(text("SELECT embedding FROM embeddings_vec_local")).scalar_one()

    assert meta_count == 1
    assert vec_count == 1
    # The blob round-trips back to the *second* upsert's vector,
    # proving the old row was dropped (not appended) before INSERT.
    assert _blob_to_vec(stored) == _make_vector(1024, fill=0.5)


def test_upsert_keeps_separate_model_versions(
    store: SqliteVecStore,
    engine: Engine,
) -> None:
    """Different ``model_version`` values yield distinct rows.

    Phase 1 docstring on :meth:`VectorStore.upsert` calls this out
    explicitly: "a single entity can hold multiple vectors when
    migrating between models". Pinning it here prevents an
    over-eager natural-key delete from collapsing model versions.
    """
    v1 = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="bge-m3",
        model_version="v1",
        vector=_make_vector(1024, fill=0.1),
        created_at=_ts(),
    )
    v2 = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="bge-m3",
        model_version="v2",
        vector=_make_vector(1024, fill=0.2),
        created_at=_ts(),
    )
    store.upsert([v1, v2])

    with engine.connect() as conn:
        meta_count = conn.execute(text("SELECT COUNT(*) FROM embeddings")).scalar_one()
        vec_count = conn.execute(text("SELECT COUNT(*) FROM embeddings_vec_local")).scalar_one()

    assert meta_count == 2
    assert vec_count == 2


def test_upsert_routes_by_dim_to_correct_vec_table(
    store: SqliteVecStore,
    engine: Engine,
) -> None:
    """A dim-1024 embedding lands in ``embeddings_vec_local``; dim-1536 in ``...openai``."""
    local = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="bge-m3",
        model_version="v1",
        vector=_make_vector(1024, fill=0.1),
        created_at=_ts(),
    )
    openai = StoredEmbedding(
        entity_type="task",
        entity_id="01J1",
        model_id="openai:text-embedding-3-small",
        model_version="2026-05-01",
        vector=_make_vector(1536, fill=0.1),
        created_at=_ts(),
    )
    store.upsert([local, openai])

    with engine.connect() as conn:
        local_count = conn.execute(text("SELECT COUNT(*) FROM embeddings_vec_local")).scalar_one()
        openai_count = conn.execute(text("SELECT COUNT(*) FROM embeddings_vec_openai")).scalar_one()
        voyage_count = conn.execute(text("SELECT COUNT(*) FROM embeddings_vec_voyage")).scalar_one()

    assert local_count == 1
    assert openai_count == 1
    # No accidental cross-write into the unused voyage table — the
    # routing table maps dim=1024 to ``embeddings_vec_local`` only.
    assert voyage_count == 0


# ---- recall ---------------------------------------------------------------


def test_recall_returns_nearest_neighbours_in_score_descending_order(
    store: SqliteVecStore,
) -> None:
    """Recall returns hits ordered ``score`` descending (closest first).

    We seed five vectors at distinct constant fills so pairwise
    Euclidean distance is a strict monotone function of the fill
    difference. Querying near fill=0.5 must therefore put the fill=0.5
    row first, then 0.4 / 0.6, etc.
    """
    fills = [0.1, 0.3, 0.5, 0.7, 0.9]
    embeddings = [
        StoredEmbedding(
            entity_type="task",
            entity_id=f"01J{i}",
            model_id="bge-m3",
            model_version="v1",
            vector=_make_vector(1024, fill=fill),
            created_at=_ts(),
        )
        for i, fill in enumerate(fills)
    ]
    store.upsert(embeddings)

    query = _make_vector(1024, fill=0.5)
    hits = store.recall(query, k=3)

    assert len(hits) == 3
    # First hit must be the exact match.
    assert hits[0].entity_id == "01J2"
    # Score is monotone decreasing in distance → list is sorted desc.
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), (
        f"recall must order hits by score desc; got {scores!r}"
    )


def test_recall_filters_by_entity_type(store: SqliteVecStore) -> None:
    """``entity_types=["task"]`` drops source hits even when they are closer.

    The plan §2.1 spec is explicit that entity_type filtering happens
    post-query, so we don't make assertions about the number of hits
    relative to ``k`` after filtering — only that no disallowed
    entity_type leaks through.
    """
    rows: list[StoredEmbedding] = []
    for i in range(3):
        rows.append(
            StoredEmbedding(
                entity_type="task",
                entity_id=f"task-{i}",
                model_id="bge-m3",
                model_version="v1",
                vector=_make_vector(1024, fill=0.1 + i * 0.01),
                created_at=_ts(),
            )
        )
    for i in range(3):
        rows.append(
            StoredEmbedding(
                entity_type="source",
                entity_id=f"src-{i}",
                model_id="bge-m3",
                model_version="v1",
                vector=_make_vector(1024, fill=0.2 + i * 0.01),
                created_at=_ts(),
            )
        )
    store.upsert(rows)

    hits = store.recall(_make_vector(1024, fill=0.15), k=6, entity_types=["task"])

    assert len(hits) >= 1
    assert all(h.entity_type == "task" for h in hits), (
        f"entity_types filter leaked non-task hits: {[h.entity_type for h in hits]!r}"
    )


def test_recall_k_zero_returns_empty_list(store: SqliteVecStore) -> None:
    """``k=0`` short-circuits without calling vec0.

    vec0 rejects ``k = 0`` with an OperationalError, so the store must
    short-circuit. Tests this guard explicitly because callers in
    Phase 4.x may pass through a user-supplied ``k``.
    """
    store.upsert(
        [
            StoredEmbedding(
                entity_type="task",
                entity_id="01J0",
                model_id="bge-m3",
                model_version="v1",
                vector=_make_vector(1024, fill=0.1),
                created_at=_ts(),
            )
        ]
    )
    assert store.recall(_make_vector(1024, fill=0.1), k=0) == []


def test_recall_returns_vector_blob_intact(store: SqliteVecStore) -> None:
    """The vector returned by recall round-trips bit-exact via float32 packing.

    sqlite-vec stores vectors as little-endian float32; the recall
    path must surface the same bytes the upsert path wrote. Bit-exact
    equality is achievable because the fill values we use are exactly
    representable in float32.
    """
    fill = 0.5  # 0.5 has an exact float32 representation
    store.upsert(
        [
            StoredEmbedding(
                entity_type="task",
                entity_id="01J0",
                model_id="bge-m3",
                model_version="v1",
                vector=_make_vector(1024, fill=fill),
                created_at=_ts(),
            )
        ]
    )
    hits = store.recall(_make_vector(1024, fill=fill), k=1)

    assert len(hits) == 1
    assert hits[0].vector == _make_vector(1024, fill=fill)


# ---- count ----------------------------------------------------------------


def test_count_returns_total_and_per_type(store: SqliteVecStore) -> None:
    """``count()`` returns total rows; ``count(entity_type=...)`` filters."""
    rows: list[StoredEmbedding] = []
    for i in range(3):
        rows.append(
            StoredEmbedding(
                entity_type="task",
                entity_id=f"task-{i}",
                model_id="bge-m3",
                model_version="v1",
                vector=_make_vector(1024, fill=0.1 + i * 0.01),
                created_at=_ts(),
            )
        )
    for i in range(2):
        rows.append(
            StoredEmbedding(
                entity_type="source",
                entity_id=f"src-{i}",
                model_id="bge-m3",
                model_version="v1",
                vector=_make_vector(1024, fill=0.2 + i * 0.01),
                created_at=_ts(),
            )
        )
    store.upsert(rows)

    assert store.count() == 5
    assert store.count(entity_type="task") == 3
    assert store.count(entity_type="source") == 2
    # Unknown types are not errors — they just count as zero.
    assert store.count(entity_type="missing") == 0


# ---- delete ---------------------------------------------------------------


def test_delete_removes_metadata_and_all_vec_rows_for_entity(
    store: SqliteVecStore,
    engine: Engine,
) -> None:
    """Delete clears every (model_id, model_version) row for the entity.

    Verifies the lock-step contract: metadata rows and their matching
    vec0 rows go away together. An orphan vec0 row would leak into
    future ``recall`` results with no JOIN target on the metadata
    side, surfacing as ``None`` columns.
    """
    rows = [
        StoredEmbedding(
            entity_type="task",
            entity_id="01J0",
            model_id="bge-m3",
            model_version="v1",
            vector=_make_vector(1024, fill=0.1),
            created_at=_ts(),
        ),
        StoredEmbedding(
            entity_type="task",
            entity_id="01J0",
            model_id="bge-m3",
            model_version="v2",
            vector=_make_vector(1024, fill=0.2),
            created_at=_ts(),
        ),
        # Unrelated entity to confirm the WHERE clause is narrow enough.
        StoredEmbedding(
            entity_type="task",
            entity_id="01J1",
            model_id="bge-m3",
            model_version="v1",
            vector=_make_vector(1024, fill=0.3),
            created_at=_ts(),
        ),
    ]
    store.upsert(rows)

    deleted = store.delete(entity_type="task", entity_id="01J0")
    assert deleted == 2

    with engine.connect() as conn:
        remaining_meta = conn.execute(
            text("SELECT entity_id FROM embeddings ORDER BY entity_id")
        ).all()
        remaining_vec = conn.execute(text("SELECT rowid FROM embeddings_vec_local")).all()

    assert [row.entity_id for row in remaining_meta] == ["01J1"]
    # The unrelated entity has exactly one vec0 row left.
    assert len(remaining_vec) == 1


def test_delete_missing_entity_returns_zero(store: SqliteVecStore) -> None:
    """Deleting an entity that was never embedded is a no-op."""
    assert store.delete(entity_type="task", entity_id="never") == 0


# ---- failure modes --------------------------------------------------------


def test_unsupported_dim_raises_config_error(store: SqliteVecStore) -> None:
    """Upserting a dim outside ``VEC_TABLES_BY_DIM`` raises with the supported set.

    The error message must list the supported dimensions so an
    operator hitting it knows whether they need a migration + map
    entry or a different embedder.
    """
    bad = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="mystery",
        model_version="v1",
        vector=_make_vector(999, fill=0.1),
        created_at=_ts(),
    )
    with pytest.raises(ConfigError) as exc_info:
        store.upsert([bad])

    msg = str(exc_info.value)
    assert "999" in msg
    for dim in VEC_TABLES_BY_DIM:
        assert str(dim) in msg, f"error must list supported dim {dim}: {msg!r}"


def test_recall_unsupported_dim_raises_config_error(store: SqliteVecStore) -> None:
    """A query vector with no matching vec0 table fails fast with ConfigError.

    Routing applies to recall too — the alternative (silent empty
    result) would hide a config bug behind "no nearest neighbours".
    """
    with pytest.raises(ConfigError):
        store.recall(_make_vector(999), k=1)


# ---- helper-level round-trip ---------------------------------------------


def test_blob_roundtrip_preserves_floats() -> None:
    """``_vec_to_blob`` ∘ ``_blob_to_vec`` is the identity on float32-exact tuples.

    Sanity-check on the wire format used by every other test in this
    module — if the helpers diverge from sqlite-vec's expectations,
    every recall assertion would fail with confusing distance values
    rather than a clear blob mismatch here.
    """
    vector = (0.0, 0.5, -0.25, 1.0, -1.0)
    blob = _vec_to_blob(vector)
    # 5 floats * 4 bytes each = 20 bytes (vec0's float32 wire format).
    assert len(blob) == 5 * 4
    # Cross-check against struct directly so we don't just retest our
    # own helper against itself.
    assert struct.unpack(f"<{len(vector)}f", blob) == vector
    assert _blob_to_vec(blob) == vector
