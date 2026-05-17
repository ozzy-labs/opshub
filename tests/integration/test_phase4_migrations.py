"""Integration tests for Phase 4 step A1 migrations.

These tests pin the physical shape of the Phase 4 vector store
provisioned by migration ``0013_create_embeddings_vec_table``:

* The Phase 1 ``vector BLOB`` column has been dropped from
  ``embeddings`` — embeddings is now a metadata-only projection.
* Three backend-specific ``vec0`` virtual tables exist
  (``embeddings_vec_local`` / ``embeddings_vec_openai`` /
  ``embeddings_vec_voyage``) with the dimensions pinned in
  phase-4-plan §4 Open Q解消.
* Each ``vec0`` table accepts inserts at its declared dimension and
  rejects mismatched ones.
* Downgrade restores the Phase 1 ``vector BLOB`` column and removes
  every ``vec0`` table.

The whole module is skipped when ``sqlite_vec`` is not importable
(non-``[vector]`` environments) so contributors who run ``uv sync
--extra dev`` without the vector extras don't trip the migration with
``no such module: vec0``.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

# Skip the entire module when the sqlite-vec extension is not installed:
# migration 0013 emits ``CREATE VIRTUAL TABLE ... USING vec0`` which
# requires the extension to be loaded on the migration connection. The
# engine factory loads it automatically when ``opshub[vector]`` is
# installed; without it, the migration cannot run and these assertions
# would fail with confusing "no such module" errors.
pytest.importorskip("sqlite_vec")

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from opshub.db.engine import create_engine_for_sqlite

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"

# Mirrors the constant in migration 0013. We re-state it here rather
# than importing because tests should fail loudly when the migration's
# layout drifts — an import would silently track the change.
_VEC_TABLES: tuple[tuple[str, int], ...] = (
    ("embeddings_vec_local", 1024),
    ("embeddings_vec_openai", 1536),
    ("embeddings_vec_voyage", 1024),
)


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper used in ``tests/integration/test_phase3_migrations.py``
    so the Phase 4 tests pick up the same env.py URL-resolution path the
    production CLI uses (including the sqlite-vec extension load on the
    migration connection).
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _vector_blob(dim: int, fill: float = 0.0) -> bytes:
    """Pack a ``dim``-length little-endian float32 vector.

    sqlite-vec's ``vec0`` virtual table accepts vectors either as JSON
    arrays or as raw little-endian float32 blobs. We use the blob form
    because it is the on-the-wire format the production SqliteVecStore
    will ship in Phase 4 step A2.
    """
    return struct.pack(f"<{dim}f", *([fill] * dim))


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head``.

    ``head`` advances through migration 0013, so the three Phase 4
    ``vec0`` virtual tables exist and the Phase 1 ``vector`` column is
    gone.
    """
    db_path = tmp_path / "phase4.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_creates_three_vec_tables(head_engine: Engine) -> None:
    """All three ``embeddings_vec_<backend>`` virtual tables are queryable.

    ``sqlite_master`` is the canonical source of truth for virtual
    table presence; SQLAlchemy's ``inspector.get_table_names()`` does
    not always list virtual tables consistently across versions.
    """
    with head_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name LIKE 'embeddings_vec_%' "
                "ORDER BY name"
            )
        ).all()

    names = [row[0] for row in rows]
    # ``vec0`` shadow tables (e.g. ``embeddings_vec_local_chunks``)
    # also live under the same prefix. We assert containment rather
    # than exact equality so shadow tables don't break the test.
    for table_name, _dim in _VEC_TABLES:
        assert table_name in names, f"expected virtual table {table_name!r}; got {names!r}"

    # Each table should answer a trivial query without error — proves
    # the ``vec0`` module is loaded and the schema actually compiled.
    for table_name, _dim in _VEC_TABLES:
        with head_engine.connect() as conn:
            conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()


def test_embeddings_table_has_no_vector_column_after_upgrade(
    head_engine: Engine,
) -> None:
    """The Phase 1 ``vector BLOB`` column is gone post-upgrade.

    Phase 4 (ADR-0012 §5) moves vector bytes into the ``vec0`` virtual
    tables, leaving ``embeddings`` as a metadata-only projection. The
    surviving columns mirror migration 0002 minus ``vector``.
    """
    insp = inspect(head_engine)
    columns = {col["name"] for col in insp.get_columns("embeddings")}

    expected = {
        "entity_type",
        "entity_id",
        "model_id",
        "model_version",
        "dim",
        "created_at",
    }
    assert columns == expected, (
        f"embeddings column set drifted: got {sorted(columns)}, expected {sorted(expected)}"
    )
    assert "vector" not in columns

    # UNIQUE constraint on the embedding identity tuple must survive
    # the column drop (it lives on metadata that ``batch_alter_table``
    # copies through).
    uniques = {tuple(uq["column_names"]) for uq in insp.get_unique_constraints("embeddings")}
    assert (
        "entity_type",
        "entity_id",
        "model_id",
        "model_version",
    ) in uniques


def test_downgrade_restores_phase1_schema(tmp_path: Path) -> None:
    """Downgrading one step revives ``vector BLOB`` and drops the vec0 tables.

    Drives ``alembic upgrade head`` followed by ``downgrade -1`` so we
    land on revision ``0012_create_ingested_files_table``. After the
    downgrade:

    * Every ``embeddings_vec_<backend>`` table is gone.
    * ``embeddings`` has the ``vector`` column back (with the
      documented ``X''`` empty-blob default for any pre-existing rows).
    """
    db_path = tmp_path / "downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp = inspect(engine)
        columns = {col["name"] for col in insp.get_columns("embeddings")}
        assert "vector" in columns, (
            "downgrade should restore the Phase 1 ``vector`` column on embeddings; "
            f"got columns {sorted(columns)}"
        )

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name LIKE 'embeddings_vec_%'"
                )
            ).all()
        assert rows == [], (
            f"downgrade should drop every vec0 table; sqlite_master still lists {rows!r}"
        )
    finally:
        engine.dispose()


def test_vec0_table_accepts_correct_dimension_insert(head_engine: Engine) -> None:
    """Inserts at the declared dimension succeed; mismatched ones fail.

    Pins ADR-0012 §5: each backend's ``vec0`` table is dimension-locked
    at creation time so accidental cross-backend writes raise loudly
    rather than corrupting the index silently.
    """
    table_name, dim = _VEC_TABLES[0]  # embeddings_vec_local @ 1024

    # Correct-dimension insert is accepted.
    with head_engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO {table_name}(rowid, embedding) VALUES (1, :v)"),
            {"v": _vector_blob(dim)},
        )

    # Wrong-dimension insert is rejected. sqlite-vec surfaces dimension
    # mismatches as ``OperationalError`` from the underlying SQLite
    # extension.
    wrong_dim = dim + 1
    with pytest.raises(OperationalError):
        with head_engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO {table_name}(rowid, embedding) VALUES (2, :v)"),
                {"v": _vector_blob(wrong_dim)},
            )
