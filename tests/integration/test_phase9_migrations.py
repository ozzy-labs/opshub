"""Integration tests for the Phase 9 step A2 migration.

These tests pin the physical shape of ``sources.fingerprint`` after
migration ``0017_add_fingerprint_to_sources`` (ADR-0019 §決定 (d) /
phase-9-plan §2.1 A2):

* The column exists, is ``TEXT`` (SQLite stores it as text) and is
  ``NULLABLE`` so the four pre-existing connectors (``github`` /
  ``slack`` / ``ms365`` / ``box``) — which never populate the field —
  keep landing rows with ``fingerprint = NULL``.
* Existing ``sources`` rows survive the upgrade with
  ``fingerprint = NULL``. The migration cannot back-fill a value
  because ADR-0019 §不変条件 (b) forbids opening file bodies, and
  the Web-API connectors don't have a stat-derived token anyway.
* ``alembic downgrade -1`` cleanly drops the column without touching
  the other ``sources`` columns / indexes / unique constraint.

Mirrors the ``test_phase8_migrations.py`` fixture pattern so the
assertions exercise the real migration env.py path (not
``metadata.create_all``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite
from opshub.projections import sources_table

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"

# The revision just before 0017 — used as the downgrade target so the
# assertion stays stable when later migrations append on top of 0017.
_PRE_0017_REVISION = "0016_create_links_table"


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` bound to a tmp-scoped SQLite URL.

    Mirrors the helper used in ``test_phase8_migrations.py`` so the
    Phase 9 tests pick up the same env.py URL-resolution path the
    production CLI uses.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head``.

    ``head`` advances past migration 0017 so the
    ``sources.fingerprint`` column is present.
    """
    db_path = tmp_path / "phase9.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_alembic_upgrade_adds_nullable_fingerprint_column(head_engine: Engine) -> None:
    """``sources.fingerprint`` exists, is ``TEXT`` and is ``NULLABLE`` after upgrade.

    Inspecting via :func:`sqlalchemy.inspect` (not the in-Python
    :class:`Table` stub) makes the assertion catch a migration that
    drifted from its sibling Table stub.
    """
    insp = inspect(head_engine)
    assert "sources" in insp.get_table_names()

    columns = {col["name"]: col for col in insp.get_columns("sources")}
    assert "fingerprint" in columns, (
        f"sources.fingerprint missing after 0017; got columns {sorted(columns)!r}"
    )
    fingerprint = columns["fingerprint"]
    # ``ADD COLUMN ... TEXT`` on SQLite reflects as ``TEXT``. Comparing
    # the upper-cased name keeps the test portable across SQLAlchemy
    # versions that have flip-flopped on case preservation.
    assert str(fingerprint["type"]).upper() == "TEXT"
    assert fingerprint["nullable"] is True, (
        "fingerprint must be nullable so the four pre-existing connectors"
        " continue to land rows with NULL without a back-fill"
    )


def test_alembic_upgrade_preserves_other_sources_columns(head_engine: Engine) -> None:
    """The 0017 migration must NOT touch the other ``sources`` columns / constraints.

    Pins the boundary of the schema change: adding ``fingerprint``
    must not regress the Phase 3 column shape, the
    ``(connector_name, external_id)`` UNIQUE constraint, or the
    ``ix_sources_*`` indexes.
    """
    insp = inspect(head_engine)
    columns = {col["name"]: col for col in insp.get_columns("sources")}

    # Phase 3 columns survive untouched (nullability + presence).
    phase3_columns: dict[str, bool] = {
        "id": False,
        "connector_name": False,
        "external_id": False,
        "source_type": False,
        "title": False,
        "url": True,
        "summary": True,
        "observed_at": False,
        "updated_at": False,
    }
    for name, nullable in phase3_columns.items():
        assert name in columns, f"Phase 3 column {name!r} disappeared after 0017"
        assert columns[name]["nullable"] is nullable, f"sources.{name} nullability changed by 0017"

    # Indexes + unique constraint survive.
    sources_unique = {tuple(uq["column_names"]) for uq in insp.get_unique_constraints("sources")}
    assert ("connector_name", "external_id") in sources_unique
    sources_indexes = {idx["name"] for idx in insp.get_indexes("sources")}
    assert "ix_sources_connector_name" in sources_indexes
    assert "ix_sources_updated_at" in sources_indexes


def test_alembic_upgrade_existing_row_back_fills_null(tmp_path: Path) -> None:
    """Pre-0017 rows survive the upgrade with ``fingerprint = NULL``.

    Simulates the real-world upgrade path: an operator running Phase
    7 / Phase 8 has ``sources`` populated by the four legacy
    connectors. Migration 0017 adds the column without back-fill, so
    every existing row must show ``NULL`` after the upgrade — there
    is no operator action required.
    """
    db_path = tmp_path / "phase9_existing.sqlite"
    cfg = _make_alembic_config(db_path)

    # Stop at 0016 so we can insert a row against the pre-0017 schema,
    # then advance to 0017 and verify the existing row picked up NULL.
    command.upgrade(cfg, _PRE_0017_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        # Issue raw SQL listing the pre-0017 column set explicitly so
        # SQLAlchemy does not try to bind ``fingerprint`` against the
        # in-Python ``sources_table`` stub (which already declares the
        # column). This is the only way the test can write a "Phase
        # 8-era" row against a Phase 8-era schema without depending on
        # the pre-0017 reflection of the table.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sources ("
                    "id, connector_name, external_id, source_type, title,"
                    " url, summary, observed_at, updated_at"
                    ") VALUES ("
                    ":id, :connector_name, :external_id, :source_type, :title,"
                    " :url, :summary, :observed_at, :updated_at"
                    ")"
                ),
                {
                    "id": "01HA000000000000000000ABCD",
                    "connector_name": "github",
                    "external_id": "owner/repo#42",
                    "source_type": "issue",
                    "title": "legacy row inserted pre-0017",
                    "url": None,
                    "summary": None,
                    "observed_at": now,
                    "updated_at": now,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).mappings().one()
        assert row["fingerprint"] is None, (
            "existing pre-0017 rows must read back as NULL after the upgrade"
        )
        assert row["title"] == "legacy row inserted pre-0017", (
            "0017 must not touch other columns of pre-existing rows"
        )
    finally:
        engine.dispose()


def test_alembic_downgrade_drops_fingerprint_column(tmp_path: Path) -> None:
    """Downgrading past 0017 removes the column cleanly.

    Drives ``alembic upgrade head`` followed by an explicit
    ``downgrade`` to ``0016_create_links_table`` so we land just
    before Phase 9 step A2. After downgrade:

    * ``sources.fingerprint`` is gone.
    * Every other ``sources`` column survives.
    * The ``(connector_name, external_id)`` UNIQUE constraint and the
      ``ix_sources_*`` indexes survive.
    * Every prior projection table (Phase 1-8) still exists.

    Targeting a named revision (not ``-1``) keeps the assertion
    stable as new migrations append on top of 0017 — mirrors the
    Phase 4 / Phase 5 / Phase 6 / Phase 8 patterns.
    """
    db_path = tmp_path / "phase9_downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_up = inspect(engine)
        up_columns = {col["name"] for col in insp_up.get_columns("sources")}
        assert "fingerprint" in up_columns
    finally:
        engine.dispose()

    command.downgrade(cfg, _PRE_0017_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        insp_down = inspect(engine)

        # Column gone.
        down_columns = {col["name"] for col in insp_down.get_columns("sources")}
        assert "fingerprint" not in down_columns, "0017 downgrade must drop the fingerprint column"

        # Other columns survived.
        for name in (
            "id",
            "connector_name",
            "external_id",
            "source_type",
            "title",
            "url",
            "summary",
            "observed_at",
            "updated_at",
        ):
            assert name in down_columns, (
                f"sources.{name} was unexpectedly dropped by the 0017 downgrade"
            )

        # UNIQUE + indexes survived.
        sources_unique = {
            tuple(uq["column_names"]) for uq in insp_down.get_unique_constraints("sources")
        }
        assert ("connector_name", "external_id") in sources_unique
        sources_indexes = {idx["name"] for idx in insp_down.get_indexes("sources")}
        assert "ix_sources_connector_name" in sources_indexes
        assert "ix_sources_updated_at" in sources_indexes

        # Prior projection tables intact (Phase 1-8).
        down_tables = set(insp_down.get_table_names())
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
            "links",
        ):
            assert prior in down_tables, (
                f"prior table {prior!r} was unexpectedly dropped by the 0017 downgrade"
            )
    finally:
        engine.dispose()
