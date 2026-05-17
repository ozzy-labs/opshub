"""Integration tests for the Phase 3 step A2 migrations.

These tests pin the physical shape of the Phase 3 read-model tables
provisioned by migrations ``0010_create_sources_table`` and
``0011_create_connector_cursors_table``:

* Column names / types / nullability.
* Index presence (``sources`` only).
* The ``UNIQUE(connector_name, external_id)`` constraint on ``sources``
  that powers the upsert semantics required by phase-3-plan §3 機能 §3.
* Clean downgrade — both new tables vanish, prior tables intact.

They use the same fixture pattern as
``tests/integration/test_projections_rebuild.py``: a tmp-scoped SQLite
file driven through Alembic so the assertions exercise the real
migration env.py path, not ``metadata.create_all``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from opshub.db.engine import create_engine_for_sqlite
from opshub.projections import sources_table

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper used in ``test_projections_rebuild.py`` so the
    Phase 3 tests pick up the same env.py URL-resolution path the
    production CLI uses.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head``.

    Both Phase 3 tables (``sources`` / ``connector_cursors``) are
    present because ``head`` advances past 0011.
    """
    db_path = tmp_path / "phase3.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_phase3_migrations_create_expected_columns(head_engine: Engine) -> None:
    """``sources`` / ``connector_cursors`` have the documented column shape.

    We inspect the live schema via :func:`sqlalchemy.inspect` rather
    than comparing against the in-Python :class:`Table` definitions so
    the test catches a migration that drifted from its sibling Table
    stub.
    """
    insp = inspect(head_engine)

    # --- sources ----------------------------------------------------------
    assert "sources" in insp.get_table_names()
    sources_columns = {col["name"]: col for col in insp.get_columns("sources")}

    expected_sources: dict[str, dict[str, object]] = {
        "id": {"nullable": False},
        "connector_name": {"nullable": False},
        "external_id": {"nullable": False},
        "source_type": {"nullable": False},
        "title": {"nullable": False},
        "url": {"nullable": True},
        "summary": {"nullable": True},
        "observed_at": {"nullable": False},
        "updated_at": {"nullable": False},
    }
    assert set(sources_columns) == set(expected_sources), (
        f"sources column set mismatch; got {sorted(sources_columns)}"
    )
    for name, expected in expected_sources.items():
        assert sources_columns[name]["nullable"] == expected["nullable"], (
            f"sources.{name} nullability mismatch: got {sources_columns[name]['nullable']!r}"
        )

    sources_pk = insp.get_pk_constraint("sources")
    assert sources_pk["constrained_columns"] == ["id"]

    sources_unique = {tuple(uq["column_names"]) for uq in insp.get_unique_constraints("sources")}
    assert ("connector_name", "external_id") in sources_unique

    sources_indexes = {idx["name"] for idx in insp.get_indexes("sources")}
    assert "ix_sources_connector_name" in sources_indexes
    assert "ix_sources_updated_at" in sources_indexes

    # --- connector_cursors ------------------------------------------------
    assert "connector_cursors" in insp.get_table_names()
    cursors_columns = {col["name"]: col for col in insp.get_columns("connector_cursors")}

    expected_cursors: dict[str, dict[str, object]] = {
        "connector_name": {"nullable": False},
        "cursor_value": {"nullable": True},
        "updated_at": {"nullable": False},
        "last_synced_at": {"nullable": False},
    }
    assert set(cursors_columns) == set(expected_cursors), (
        f"connector_cursors column set mismatch; got {sorted(cursors_columns)}"
    )
    for name, expected in expected_cursors.items():
        assert cursors_columns[name]["nullable"] == expected["nullable"], (
            f"connector_cursors.{name} nullability mismatch: "
            f"got {cursors_columns[name]['nullable']!r}"
        )

    cursors_pk = insp.get_pk_constraint("connector_cursors")
    assert cursors_pk["constrained_columns"] == ["connector_name"]

    # No secondary indexes on connector_cursors — PK covers the only
    # access pattern.
    assert insp.get_indexes("connector_cursors") == []


def test_sources_unique_constraint_on_connector_and_external_id(
    head_engine: Engine,
) -> None:
    """A second insert with the same ``(connector_name, external_id)`` fails.

    Pins ADR-0002 / phase-3-plan §3 機能 §3: re-observation of the
    same external item must collide on the unique constraint so the
    connector is forced through the update path rather than appending
    a duplicate row.
    """
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    row = {
        "connector_name": "github",
        "external_id": "owner/repo#42",
        "source_type": "issue",
        "title": "first observation",
        "url": "https://github.com/owner/repo/issues/42",
        "summary": None,
        "observed_at": now,
        "updated_at": now,
    }

    with head_engine.begin() as conn:
        conn.execute(sources_table.insert().values(id="01HA000000000000000000AAAA", **row))

    # Second insert with the *same* (connector_name, external_id) but a
    # different ``id`` and ``title`` must raise IntegrityError. We use
    # a fresh transaction so the first row stays committed and the
    # constraint check is unambiguous.
    with pytest.raises(IntegrityError):
        with head_engine.begin() as conn:
            conn.execute(
                sources_table.insert().values(
                    id="01HA000000000000000000AAAB",
                    connector_name="github",
                    external_id="owner/repo#42",
                    source_type="issue",
                    title="second observation",
                    url=None,
                    summary=None,
                    observed_at=now,
                    updated_at=now,
                )
            )

    # Sanity: a row with a *different* external_id under the same
    # connector is allowed (the uniqueness is composite, not on
    # ``connector_name`` alone).
    with head_engine.begin() as conn:
        conn.execute(
            sources_table.insert().values(
                id="01HA000000000000000000AAAC",
                connector_name="github",
                external_id="owner/repo#43",
                source_type="issue",
                title="different item",
                url=None,
                summary=None,
                observed_at=now,
                updated_at=now,
            )
        )


def test_migrations_downgrade_phase3_tables(tmp_path: Path) -> None:
    """Downgrading the Phase 3 revisions removes only those tables.

    Drives ``alembic upgrade head`` then downgrades far enough to
    rewind past every Phase 4 migration AND the three Phase 3 tables.
    After downgrade the chain should rest on
    ``0009_create_handoffs_table``: ``sources``,
    ``connector_cursors`` and ``ingested_files`` must be gone, every
    prior projection table must remain.

    To stay revision-agnostic we downgrade explicitly to revision
    ``0009_create_handoffs_table``. The earlier ``-3`` step count
    broke when Phase 4 migration 0013 landed; targeting a named
    revision keeps the assertion stable as new migrations append.
    """
    db_path = tmp_path / "downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_up = inspect(engine)
        up_tables = set(insp_up.get_table_names())
        assert "sources" in up_tables
        assert "connector_cursors" in up_tables
        assert "ingested_files" in up_tables
    finally:
        engine.dispose()

    # Roll back to the last Phase 2 revision so every Phase 3 table is
    # dropped regardless of how many Phase 4 migrations sit on top of
    # them.
    command.downgrade(cfg, "0009_create_handoffs_table")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_down = inspect(engine)
        down_tables = set(insp_down.get_table_names())
        assert "sources" not in down_tables
        assert "connector_cursors" not in down_tables
        assert "ingested_files" not in down_tables
        # Prior projection tables must survive the partial downgrade.
        for prior in (
            "events",
            "embeddings",
            "tasks",
            "inbox_items",
            "decisions",
            "work_sessions",
            "agent_runs",
            "locks",
            "handoffs",
        ):
            assert prior in down_tables, (
                f"prior table {prior!r} was unexpectedly dropped by the Phase 3 downgrade"
            )
    finally:
        engine.dispose()
