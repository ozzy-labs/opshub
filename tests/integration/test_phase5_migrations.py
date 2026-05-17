"""Integration tests for the Phase 5 step B2 migration.

These tests pin the physical shape of the ``briefings`` projection
table provisioned by migration ``0014_create_briefings_table``:

* Column names / types / nullability.
* Primary key on ``id``.
* The :func:`all_projections` registry actually wires
  :class:`BriefingsProjection` in, so a rebuild against a freshly
  migrated DB can write briefing rows without surprise (and runs
  without error against the empty event log).
* Clean downgrade — the ``briefings`` table vanishes, prior tables
  intact.

Mirrors the Phase 3 / Phase 4 migration test fixtures so the
assertions exercise the real migration env.py path, not
``metadata.create_all``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.projections import briefings_table
from opshub.projections.rebuild import rebuild_all
from opshub.projections.registry import all_projections

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper used in ``test_phase3_migrations.py`` /
    ``test_phase4_migrations.py`` so the Phase 5 tests pick up the
    same env.py URL-resolution path the production CLI uses.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head``.

    ``head`` advances past migration 0014 so the ``briefings`` table
    is present.
    """
    db_path = tmp_path / "phase5.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_alembic_upgrade_creates_briefings_table(head_engine: Engine) -> None:
    """``briefings`` has the documented column shape after ``upgrade head``.

    Inspect the live schema via :func:`sqlalchemy.inspect` rather than
    comparing against the in-Python :class:`Table` definition so the
    test catches a migration that drifted from its sibling Table stub.
    """
    insp = inspect(head_engine)

    assert "briefings" in insp.get_table_names()
    columns = {col["name"]: col for col in insp.get_columns("briefings")}

    expected: dict[str, dict[str, object]] = {
        "id": {"nullable": False},
        "topic": {"nullable": False},
        "scope": {"nullable": False},
        "markdown": {"nullable": False},
        "source_refs": {"nullable": False},
        "model_id": {"nullable": False},
        "model_version": {"nullable": True},
        "tokens_in": {"nullable": False},
        "tokens_out": {"nullable": False},
        "generated_at": {"nullable": False},
    }
    assert set(columns) == set(expected), (
        f"briefings column set mismatch; got {sorted(columns)}, expected {sorted(expected)}"
    )
    for name, attrs in expected.items():
        assert columns[name]["nullable"] == attrs["nullable"], (
            f"briefings.{name} nullability mismatch: got {columns[name]['nullable']!r}"
        )

    pk = insp.get_pk_constraint("briefings")
    assert pk["constrained_columns"] == ["id"]


def test_projections_rebuild_includes_briefings(head_engine: Engine) -> None:
    """``all_projections()`` includes ``briefings`` and ``rebuild_all`` runs cleanly.

    Pins two contracts at once:

    1. The :func:`all_projections` registry actually wires
       :class:`BriefingsProjection` in — without this entry the
       briefing CLI's rebuild path would silently leave the table
       stale.
    2. ``rebuild_all`` against a freshly migrated DB with an empty
       event log completes without error and leaves the table empty.
       This catches a class of bug where ``reset`` is wired to a
       non-existent table or the projection raises on first reset.
    """
    names = [projection.name for projection in all_projections()]
    assert "briefings" in names, f"all_projections() must include 'briefings'; got {names!r}"

    store = SqlAlchemyEventStore(head_engine)
    rebuild_all(head_engine, store, all_projections())

    with head_engine.connect() as conn:
        rows = conn.execute(select(briefings_table)).all()
    assert rows == [], "rebuild from empty event log must leave briefings empty"


def test_migration_downgrade_drops_briefings_table(tmp_path: Path) -> None:
    """Downgrading one step removes the ``briefings`` table only.

    Drives ``alembic upgrade head`` then ``downgrade -1`` so we land
    on revision ``0013_create_embeddings_vec_table``. After downgrade:

    * ``briefings`` is gone.
    * Every prior projection table survives.
    """
    db_path = tmp_path / "phase5_downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_up = inspect(engine)
        assert "briefings" in insp_up.get_table_names()
    finally:
        engine.dispose()

    command.downgrade(cfg, "-1")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_down = inspect(engine)
        down_tables = set(insp_down.get_table_names())
        assert "briefings" not in down_tables
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
            "sources",
            "connector_cursors",
            "ingested_files",
        ):
            assert prior in down_tables, (
                f"prior table {prior!r} was unexpectedly dropped by the Phase 5 downgrade"
            )
    finally:
        engine.dispose()
