"""Integration tests for the Phase 10 step A2 migration.

Pin the physical shape of the ``sources`` body / provenance columns at
the **post-0018, pre-0030** revision so the original Phase 10 contract
stays test-pinned even after the epic #470 / issue #481 follow-up
(migration ``0030_enforce_sources_body_not_null``) promoted ``body`` to
``NOT NULL``:

* ``body`` / ``provenance_origin`` / ``provenance_trust`` exist, are
  ``TEXT`` and ``NULLABLE`` at the 0018 boundary so Phase 3-9 rows kept
  landing with ``NULL`` until the body NOT NULL rebuild.
* Existing pre-0018 rows survive the upgrade with the three columns
  ``NULL`` (no back-fill — backward-compat, ADR-0020 §(d)).
* ``alembic downgrade`` cleanly drops the three columns without touching
  the rest of the ``sources`` shape.

The post-0030 column shape (``body NOT NULL``) is pinned separately by
``test_phase3_migrations.py::test_phase3_migrations_create_expected_columns``.

Mirrors ``test_phase9_migrations.py`` so the assertions exercise the
real migration env.py path (not ``metadata.create_all``).
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

# The revision just before 0018 — used as the downgrade target so the
# assertion stays stable when later migrations append on top of 0018.
_PRE_0018_REVISION = "0017_add_fingerprint_to_sources"

# The revision the original Phase 10 contract pinned: post-0018 +
# subsequent additive revisions but before the epic #470 / issue #481
# ``body NOT NULL`` rebuild (migration ``0030``). Test fixtures upgrade
# to this revision so the original Phase 10 nullability + existing-row
# ``NULL`` body contracts stay observable.
_POST_0018_PRE_0030_REVISION = "0029_create_slack_demand_digest"

_NEW_COLUMNS = ("body", "provenance_origin", "provenance_trust")


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at the post-0018, pre-0030 revision.

    The original Phase 10 contract pinned ``body`` / ``provenance_*`` as
    nullable. Epic #470 / issue #481 (migration 0030) later promoted
    ``body`` to ``NOT NULL``; this test exercises the 0018-era contract
    so it stays observable. The post-0030 column shape lives in
    ``test_phase3_migrations.py``.
    """
    db_path = tmp_path / "phase10.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, _POST_0018_PRE_0030_REVISION)
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_adds_nullable_body_provenance_columns(head_engine: Engine) -> None:
    """The three Phase 10 columns exist, are ``TEXT`` and ``NULLABLE``."""
    insp = inspect(head_engine)
    columns = {col["name"]: col for col in insp.get_columns("sources")}
    for name in _NEW_COLUMNS:
        assert name in columns, f"sources.{name} missing after 0018; got {sorted(columns)!r}"
        assert str(columns[name]["type"]).upper() == "TEXT"
        assert columns[name]["nullable"] is True, (
            f"sources.{name} must be nullable so Phase 3-9 rows keep landing NULL"
        )


def test_upgrade_preserves_existing_sources_shape(head_engine: Engine) -> None:
    """0018 must not regress the Phase 3 / Phase 9 column shape or constraints."""
    insp = inspect(head_engine)
    columns = {col["name"] for col in insp.get_columns("sources")}
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
        "fingerprint",
    ):
        assert name in columns, f"sources.{name} disappeared after 0018"

    sources_unique = {tuple(uq["column_names"]) for uq in insp.get_unique_constraints("sources")}
    assert ("connector_name", "external_id") in sources_unique
    sources_indexes = {idx["name"] for idx in insp.get_indexes("sources")}
    assert "ix_sources_connector_name" in sources_indexes
    assert "ix_sources_updated_at" in sources_indexes


def test_upgrade_existing_row_reads_null(tmp_path: Path) -> None:
    """A pre-0018 row survives the upgrade with body / provenance ``NULL``."""
    db_path = tmp_path / "phase10_existing.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, _PRE_0018_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
        with engine.begin() as conn:
            # Explicit pre-0018 column list (includes fingerprint from
            # 0017, excludes the three columns 0018 adds) so the insert
            # matches the on-disk schema at this revision.
            conn.execute(
                text(
                    "INSERT INTO sources ("
                    "id, connector_name, external_id, source_type, title,"
                    " url, summary, observed_at, updated_at, fingerprint"
                    ") VALUES ("
                    ":id, :connector_name, :external_id, :source_type, :title,"
                    " :url, :summary, :observed_at, :updated_at, :fingerprint"
                    ")"
                ),
                {
                    "id": "01HB000000000000000000ABCD",
                    "connector_name": "slack",
                    "external_id": "C1:1700000000.0001",
                    "source_type": "slack_message",
                    "title": "legacy row inserted pre-0018",
                    "url": None,
                    "summary": "preview only (Phase 9 era)",
                    "observed_at": now,
                    "updated_at": now,
                    "fingerprint": None,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, _POST_0018_PRE_0030_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).mappings().one()
        assert row["body"] is None
        assert row["provenance_origin"] is None
        assert row["provenance_trust"] is None
        assert row["title"] == "legacy row inserted pre-0018"
        assert row["summary"] == "preview only (Phase 9 era)"
    finally:
        engine.dispose()


def test_downgrade_drops_body_provenance_columns(tmp_path: Path) -> None:
    """Downgrading past 0018 removes exactly the three new columns."""
    db_path = tmp_path / "phase10_downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, _POST_0018_PRE_0030_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        up_columns = {col["name"] for col in inspect(engine).get_columns("sources")}
        for name in _NEW_COLUMNS:
            assert name in up_columns
    finally:
        engine.dispose()

    command.downgrade(cfg, _PRE_0018_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        insp = inspect(engine)
        down_columns = {col["name"] for col in insp.get_columns("sources")}
        for name in _NEW_COLUMNS:
            assert name not in down_columns, f"0018 downgrade must drop sources.{name}"

        # Pre-0018 columns survive (including fingerprint from 0017).
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
            "fingerprint",
        ):
            assert name in down_columns, f"sources.{name} unexpectedly dropped by 0018 downgrade"

        sources_unique = {
            tuple(uq["column_names"]) for uq in insp.get_unique_constraints("sources")
        }
        assert ("connector_name", "external_id") in sources_unique
    finally:
        engine.dispose()
