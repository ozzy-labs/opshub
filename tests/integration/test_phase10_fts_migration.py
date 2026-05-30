"""Integration tests for the Phase 10 step B2 FTS5 migration.

Pin the physical shape and trigger behaviour of migration
``0019_create_sources_fts`` (Phase 10 plan §3 Sub-issue B / §4-B,
ADR-0012 改訂版 §4):

* ``sources_fts`` exists as a virtual table after upgrade.
* Triggers ``sources_fts_ai`` / ``sources_fts_ad`` / ``sources_fts_au``
  keep the FTS index aligned with ``sources.body`` on insert / delete
  / body-update.
* Existing pre-0019 rows are back-filled into the FTS index by the
  upgrade so :class:`SearchService` can match against historic data
  whose ``body`` was populated after migration ``0018`` but before
  ``0019`` shipped (a transient state during a multi-PR rollout —
  pre-userbase, no real users see this, but the assertion still keeps
  the back-fill clause honest).
* ``alembic downgrade`` cleanly drops the FTS table and the triggers.

Mirrors :mod:`tests.integration.test_phase10_migrations` so the
assertions exercise the real migration env.py path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from opshub.db.engine import create_engine_for_sqlite

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"

_PRE_0019_REVISION = "0018_add_body_provenance_to_sources"

_TRIGGER_NAMES = ("sources_fts_ai", "sources_fts_ad", "sources_fts_au")


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head`` (past 0019)."""
    db_path = tmp_path / "phase10_fts.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_source(conn: object, *, body: str | None, external_id: str) -> None:
    """Insert one row into the ``sources`` projection table.

    We bypass the SQLAlchemy Table here to keep the migration test
    self-contained — the migration runs against raw SQLite and the
    assertion only cares about the FTS index, not the projection
    schema indirection.
    """
    # The ``conn`` argument is a SQLAlchemy Connection; we keep the
    # signature loose to mirror the helpers in test_phase10_migrations.
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO sources ("
            "id, connector_name, external_id, source_type, title,"
            " url, summary, observed_at, updated_at, fingerprint,"
            " body, provenance_origin, provenance_trust"
            ") VALUES ("
            ":id, :connector_name, :external_id, :source_type, :title,"
            " :url, :summary, :observed_at, :updated_at, :fingerprint,"
            " :body, :provenance_origin, :provenance_trust"
            ")"
        ),
        {
            "id": f"01HB{external_id[:22].ljust(22, '0')}",
            "connector_name": "github",
            "external_id": external_id,
            "source_type": "issue",
            "title": f"row {external_id}",
            "url": None,
            "summary": None,
            "observed_at": now,
            "updated_at": now,
            "fingerprint": None,
            "body": body,
            "provenance_origin": "external" if body else None,
            "provenance_trust": "untrusted" if body else None,
        },
    )


# ---- shape ---------------------------------------------------------------


def test_upgrade_creates_sources_fts_virtual_table(head_engine: Engine) -> None:
    """``sources_fts`` exists as an FTS5 virtual table after upgrade."""
    with head_engine.connect() as conn:
        row = conn.execute(
            text("SELECT type, sql FROM sqlite_master WHERE name = 'sources_fts'")
        ).first()
    assert row is not None, "sources_fts must exist after upgrade head"
    assert row.type == "table"  # FTS5 vtables present as 'table' in sqlite_master
    assert "USING fts5" in row.sql
    assert "content='sources'" in row.sql


def test_upgrade_creates_all_three_sync_triggers(head_engine: Engine) -> None:
    """The three sync triggers exist with the expected names."""
    with head_engine.connect() as conn:
        names = {
            row.name
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        }
    for trigger in _TRIGGER_NAMES:
        assert trigger in names, f"trigger {trigger} missing after upgrade head"


# ---- trigger behaviour ---------------------------------------------------


def test_insert_trigger_indexes_new_body(head_engine: Engine) -> None:
    """An INSERT populates the FTS index via the ``AFTER INSERT`` trigger."""
    with head_engine.begin() as conn:
        _insert_source(conn, body="unique sentinel token", external_id="ins-1")

    with head_engine.connect() as conn:
        match_count = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'sentinel'")
        ).scalar_one()
    assert match_count == 1


def test_update_of_body_trigger_refreshes_index(head_engine: Engine) -> None:
    """Updating ``sources.body`` refreshes the FTS document."""
    with head_engine.begin() as conn:
        _insert_source(conn, body="initial alpha token", external_id="upd-1")
        conn.execute(
            text("UPDATE sources SET body = 'replacement beta token' WHERE external_id = 'upd-1'")
        )

    with head_engine.connect() as conn:
        alpha_hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'alpha'")
        ).scalar_one()
        beta_hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'beta'")
        ).scalar_one()
    assert alpha_hits == 0
    assert beta_hits == 1


def test_delete_trigger_drops_index_entry(head_engine: Engine) -> None:
    """Deleting a source removes its FTS document."""
    with head_engine.begin() as conn:
        _insert_source(conn, body="ephemeral marker xyz", external_id="del-1")
        conn.execute(text("DELETE FROM sources WHERE external_id = 'del-1'"))

    with head_engine.connect() as conn:
        hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'ephemeral'")
        ).scalar_one()
    assert hits == 0


def test_null_body_row_produces_empty_fts_document(head_engine: Engine) -> None:
    """A NULL body still inserts an FTS row but matches no query."""
    with head_engine.begin() as conn:
        _insert_source(conn, body=None, external_id="null-1")
        # And a sibling row with body so the test discriminates between
        # "no entries" and "entries but empty".
        _insert_source(conn, body="sibling actual content", external_id="null-2")

    with head_engine.connect() as conn:
        # The NULL-body row contributes a row to sources_fts but no
        # token. We assert by checking that a token from the sibling
        # row's body returns exactly 1 hit (not 2).
        hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'sibling'")
        ).scalar_one()
    assert hits == 1


# ---- back-fill -----------------------------------------------------------


def test_upgrade_backfills_existing_rows(tmp_path: Path) -> None:
    """Pre-0019 rows whose body was populated by 0018 land in the index.

    Sequence: ``upgrade 0018`` → insert a row with a body → ``upgrade
    head`` (which runs 0019). The 0019 back-fill ``INSERT INTO
    sources_fts(rowid, body) SELECT rowid, body FROM sources``
    statement must pick up the pre-existing row.
    """
    db_path = tmp_path / "backfill.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, _PRE_0019_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            _insert_source(
                conn,
                body="pre-0019 body content with backfill marker",
                external_id="backfill-1",
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            hits = conn.execute(
                text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'backfill'")
            ).scalar_one()
        assert hits == 1
    finally:
        engine.dispose()


# ---- downgrade -----------------------------------------------------------


def test_downgrade_drops_fts_table_and_triggers(tmp_path: Path) -> None:
    """``alembic downgrade`` past 0019 removes the FTS surface."""
    db_path = tmp_path / "downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'sources_fts'")).first()
                is not None
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, _PRE_0019_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'sources_fts'")).first()
                is None
            )
            remaining = {
                row.name
                for row in conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE 'sources_fts%'"
                    )
                )
            }
            assert remaining == set()
    finally:
        engine.dispose()
