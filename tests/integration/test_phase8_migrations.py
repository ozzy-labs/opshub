"""Integration tests for the Phase 8 step A2 migration.

These tests pin the physical shape of the ``links`` projection
table provisioned by migration ``0016_create_links_table``:

* Column names / types / nullability.
* Primary key on ``id``.
* The ``links_natural_key_uq`` UNIQUE constraint covering the
  ADR-0017 §決定 (a) natural-key tuple
  ``(from_entity_type, from_entity_id, to_entity_type,
  to_entity_id, link_type)``.
* The two bidirectional traversal indexes (``links_from_idx`` /
  ``links_to_idx``).
* The :func:`all_projections` registry actually wires
  :class:`opshub.projections.links.LinksProjector` in, so a rebuild
  against a freshly migrated DB can pass an empty event log through
  the projector without surprise (and runs without error against the
  empty event log).
* Clean downgrade — the ``links`` table vanishes (and the two
  indexes with it), prior tables intact.

Mirrors the Phase 5 / Phase 6 migration test fixtures so the
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
from opshub.projections import links_table
from opshub.projections.rebuild import rebuild_all
from opshub.projections.registry import all_projections

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper used in ``test_phase6_migrations.py`` so the
    Phase 8 tests pick up the same env.py URL-resolution path the
    production CLI uses.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head``.

    ``head`` advances past migration 0016 so the ``links`` table and
    its two indexes are present.
    """
    db_path = tmp_path / "phase8.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_alembic_upgrade_creates_links_table(head_engine: Engine) -> None:
    """``links`` has the documented column shape after ``upgrade head``.

    Inspect the live schema via :func:`sqlalchemy.inspect` rather than
    comparing against the in-Python :class:`Table` definition so the
    test catches a migration that drifted from its sibling Table stub.
    """
    insp = inspect(head_engine)

    assert "links" in insp.get_table_names()
    columns = {col["name"]: col for col in insp.get_columns("links")}

    expected: dict[str, dict[str, object]] = {
        "id": {"nullable": False},
        "from_entity_type": {"nullable": False},
        "from_entity_id": {"nullable": False},
        "to_entity_type": {"nullable": False},
        "to_entity_id": {"nullable": False},
        "link_type": {"nullable": False},
        "created_at": {"nullable": False},
        "source_event_id": {"nullable": True},
        "metadata": {"nullable": True},
    }
    assert set(columns) == set(expected), (
        f"links column set mismatch; got {sorted(columns)}, expected {sorted(expected)}"
    )
    for name, attrs in expected.items():
        assert columns[name]["nullable"] == attrs["nullable"], (
            f"links.{name} nullability mismatch: got {columns[name]['nullable']!r}"
        )

    pk = insp.get_pk_constraint("links")
    assert pk["constrained_columns"] == ["id"]


def test_alembic_creates_natural_key_unique_constraint(head_engine: Engine) -> None:
    """``links_natural_key_uq`` covers the ADR-0017 §決定 (a) tuple.

    Phase 8 B2 will use this constraint as the conflict target for
    the SQLite UPSERT; the column order matters because the conflict
    target tuple in ``INSERT ... ON CONFLICT (...) DO UPDATE`` must
    match exactly. Drift between the migration and the projector
    would surface here, not in a runtime IntegrityError at rebuild.
    """
    insp = inspect(head_engine)
    uniques = insp.get_unique_constraints("links")

    natural_key_columns = [
        "from_entity_type",
        "from_entity_id",
        "to_entity_type",
        "to_entity_id",
        "link_type",
    ]
    matching = [uc for uc in uniques if uc["column_names"] == natural_key_columns]
    assert matching, (
        "links_natural_key_uq missing; got UNIQUE constraints: "
        f"{[uc['column_names'] for uc in uniques]!r}"
    )
    assert matching[0]["name"] == "links_natural_key_uq"


def test_alembic_creates_bidirectional_traversal_indexes(head_engine: Engine) -> None:
    """Both ``links_from_idx`` and ``links_to_idx`` exist after upgrade.

    Pinning index presence here (instead of only at the SQLAlchemy
    stub) means a migration that forgot the ``op.create_index`` calls
    is caught immediately. Index column order is checked so the query
    planner picks the index up on the leading-column WHERE used by
    Phase 8 C1's ``LinkService``.
    """
    insp = inspect(head_engine)
    # Reflected indexes carry an ``Optional[str]`` name; coerce to a
    # ``str`` key so the test reads cleanly under pyright strict.
    indexes_by_name = {str(idx["name"]): idx for idx in insp.get_indexes("links")}

    assert "links_from_idx" in indexes_by_name, (
        f"links_from_idx missing; got {sorted(indexes_by_name)!r}"
    )
    assert "links_to_idx" in indexes_by_name, (
        f"links_to_idx missing; got {sorted(indexes_by_name)!r}"
    )
    assert indexes_by_name["links_from_idx"]["column_names"] == [
        "from_entity_type",
        "from_entity_id",
    ]
    assert indexes_by_name["links_to_idx"]["column_names"] == [
        "to_entity_type",
        "to_entity_id",
    ]


def test_projections_rebuild_includes_links(head_engine: Engine) -> None:
    """``all_projections()`` includes ``links`` and ``rebuild_all`` runs cleanly.

    Pins two contracts at once:

    1. The :func:`all_projections` registry actually wires
       :class:`opshub.projections.links.LinksProjector` in — without
       this entry the inline projector wiring would silently leave
       the table stale even after Phase 8 B2's extraction lands.
    2. ``rebuild_all`` against a freshly migrated DB with an empty
       event log completes without error and leaves the table empty.
       This catches a class of bug where the A2 skeleton's
       ``reset`` / ``apply`` raises on first invocation.
    """
    names = [projection.name for projection in all_projections()]
    assert "links" in names, f"all_projections() must include 'links'; got {names!r}"

    store = SqlAlchemyEventStore(head_engine)
    rebuild_all(head_engine, store, all_projections())

    with head_engine.connect() as conn:
        rows = conn.execute(select(links_table)).all()
    assert rows == [], "rebuild from empty event log must leave links empty"


def test_alembic_downgrade_removes_links_table(tmp_path: Path) -> None:
    """Downgrading past 0016 removes the ``links`` table only.

    Drives ``alembic upgrade head`` followed by an explicit
    ``downgrade`` to revision ``0015_create_proposals_table`` so we
    land just before Phase 8 step A2. After downgrade:

    * ``links`` is gone (and its two indexes with it).
    * Every prior projection table (including the Phase 6
      ``proposals``) survives.

    Targeting a named revision (rather than ``-1``) keeps the
    assertion stable as new migrations append on top of 0016 —
    mirrors the Phase 4 / Phase 5 / Phase 6 patterns.
    """
    db_path = tmp_path / "phase8_downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_up = inspect(engine)
        assert "links" in insp_up.get_table_names()
    finally:
        engine.dispose()

    command.downgrade(cfg, "0015_create_proposals_table")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_down = inspect(engine)
        down_tables = set(insp_down.get_table_names())
        assert "links" not in down_tables
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
            "briefings",
            "proposals",
        ):
            assert prior in down_tables, (
                f"prior table {prior!r} was unexpectedly dropped by the Phase 8 downgrade"
            )
    finally:
        engine.dispose()
