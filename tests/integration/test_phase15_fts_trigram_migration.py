"""Integration tests for the Phase 15 S2 FTS5 trigram migration.

Pin the physical shape, back-fill, trigger behaviour, Japanese
substring match, and downgrade reversibility of migration
``0028_rebuild_sources_fts_trigram`` (Phase 15 plan §3 S2, epic
#338 / issue #358, ADR-0028 §Decision (a)):

* ``sources_fts`` exists as a virtual table after upgrade head and
  is configured with the ``trigram`` tokenizer.
* The three sync triggers (``sources_fts_ai`` / ``sources_fts_ad``
  / ``sources_fts_au``) are re-created by 0028 — the upgrade tears
  them down before the ``DROP TABLE`` and re-installs them after
  the rebuild, so their behaviour must remain identical to 0019.
* Existing rows whose body was populated before 0028 ran land in
  the rebuilt index via the ``INSERT INTO sources_fts(rowid, body)
  SELECT rowid, body FROM sources`` back-fill clause.
* Japanese natural-text queries (``"boxの権限"``, ``"進捗記入"``,
  ``"CDKの"``) hit by substring instead of requiring an exact
  token / prefix — the entire point of switching to ``trigram``.
* ``alembic downgrade`` restores the ``unicode61 remove_diacritics
  2`` tokenizer, re-back-fills, and re-creates the triggers, so
  operators can roll the tokenizer choice back cleanly.

Existing ``tests/integration/test_phase10_fts_migration.py`` stays
untouched: it pins the 0019 immutable contract and complements
this file (which pins 0028's supersede behaviour).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from opshub.db.engine import create_engine_for_sqlite

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"

# The revision shipped immediately before 0028. ``downgrade`` lands
# the operator back on this state with the original tokenizer.
_PRE_0028_REVISION = "0019_create_sources_fts"

_TRIGGER_NAMES = ("sources_fts_ai", "sources_fts_ad", "sources_fts_au")


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a SQLite DB at ``alembic upgrade head`` (past 0028)."""
    db_path = tmp_path / "phase15_fts_trigram.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_source(conn: Connection, *, body: str | None, external_id: str) -> None:
    """Insert one row into the ``sources`` projection table.

    We bypass the SQLAlchemy Table here to keep the migration test
    self-contained — the migration runs against raw SQLite and the
    assertion only cares about the FTS index, not the projection
    schema indirection. Mirrors the helper in
    ``test_phase10_fts_migration.py``.
    """
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
    conn.execute(
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
            "connector_name": "slack",
            "external_id": external_id,
            "source_type": "slack_message",
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


def test_upgrade_head_uses_trigram_tokenizer(head_engine: Engine) -> None:
    """``sources_fts`` is configured with the ``trigram`` tokenizer.

    SQLite stores the original ``CREATE VIRTUAL TABLE`` statement
    (including the tokenize option) in ``sqlite_master.sql`` so we
    can assert directly without parsing the FTS5 config table.
    """
    with head_engine.connect() as conn:
        row = conn.execute(
            text("SELECT type, sql FROM sqlite_master WHERE name = 'sources_fts'")
        ).first()
    assert row is not None, "sources_fts must exist after upgrade head"
    assert row.type == "table"
    assert "USING fts5" in row.sql
    assert "tokenize='trigram'" in row.sql
    # And the old tokenizer must be gone — guard against a future
    # change accidentally landing both literal strings in the DDL.
    assert "unicode61" not in row.sql


def test_upgrade_head_re_creates_all_three_sync_triggers(head_engine: Engine) -> None:
    """The three sync triggers exist after 0028 (re-created by upgrade)."""
    with head_engine.connect() as conn:
        names = {
            row.name
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        }
    for trigger in _TRIGGER_NAMES:
        assert trigger in names, f"trigger {trigger} missing after upgrade head"


# ---- trigger behaviour ---------------------------------------------------


def test_insert_trigger_indexes_new_body_after_trigram_rebuild(head_engine: Engine) -> None:
    """An INSERT after 0028 still populates the FTS index.

    The trigger SQL is identical to 0019 but the underlying vtable
    is the rebuilt trigram one, so we re-pin the contract end-to-
    end against the new index shape.
    """
    with head_engine.begin() as conn:
        _insert_source(conn, body="alpha trigram sentinel beta", external_id="ins-tri-1")

    with head_engine.connect() as conn:
        # 5-letter token is comfortably above trigram's 3-char min.
        hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'sentinel'")
        ).scalar_one()
    assert hits == 1


def test_update_of_body_trigger_refreshes_index_after_trigram_rebuild(
    head_engine: Engine,
) -> None:
    """Updating ``sources.body`` refreshes the FTS document after 0028."""
    with head_engine.begin() as conn:
        _insert_source(conn, body="initial waypoint marker", external_id="upd-tri-1")
        conn.execute(
            text(
                "UPDATE sources SET body = 'replacement landmark token'"
                " WHERE external_id = 'upd-tri-1'"
            )
        )

    with head_engine.connect() as conn:
        old_hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'waypoint'")
        ).scalar_one()
        new_hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'landmark'")
        ).scalar_one()
    assert old_hits == 0
    assert new_hits == 1


def test_delete_trigger_drops_index_entry_after_trigram_rebuild(head_engine: Engine) -> None:
    """Deleting a source removes its FTS document after 0028."""
    with head_engine.begin() as conn:
        _insert_source(conn, body="ephemeral marker quartzite", external_id="del-tri-1")
        conn.execute(text("DELETE FROM sources WHERE external_id = 'del-tri-1'"))

    with head_engine.connect() as conn:
        hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'quartzite'")
        ).scalar_one()
    assert hits == 0


# ---- back-fill -----------------------------------------------------------


def test_upgrade_backfills_existing_rows_into_trigram_index(tmp_path: Path) -> None:
    """Rows present before 0028 land in the rebuilt trigram index.

    Sequence: ``upgrade 0019`` → insert a row with a body →
    ``upgrade head`` (which runs 0028 = drop + create + back-fill +
    triggers). The back-fill clause must pick up the pre-existing
    row so operator DBs do not silently lose their FTS coverage on
    the tokenizer swap.
    """
    db_path = tmp_path / "backfill.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, _PRE_0028_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            _insert_source(
                conn,
                body="pre-0028 body content with backfill marker",
                external_id="backfill-tri-1",
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            # trigram tokenizer indexes any 3-char substring, so
            # "backfill" (8 chars) hits without prefix tricks.
            hits = conn.execute(
                text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'backfill'")
            ).scalar_one()
        assert hits == 1
    finally:
        engine.dispose()


def test_upgrade_backfills_row_count_matches_sources(tmp_path: Path) -> None:
    """Back-fill installs exactly one FTS row per ``sources`` row.

    The trigram rebuild must not silently drop or duplicate rows; we
    assert ``COUNT(*)`` parity between ``sources`` and ``sources_fts``.

    epic #470 / issue #481 (migration 0030) deletes pre-existing rows
    whose ``body IS NULL`` as part of the ``body NOT NULL`` rebuild.
    The pre-0028 seed therefore stays body-populated so the upgrade
    chain (0028 → ... → 0030) preserves every row and the FTS index
    re-back-fills 1:1 against the new table shape.
    """
    db_path = tmp_path / "backfill_count.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, _PRE_0028_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            _insert_source(conn, body="row one body alpha", external_id="bcount-1")
            _insert_source(conn, body="row two body beta", external_id="bcount-2")
            _insert_source(conn, body="row three body gamma", external_id="bcount-3")
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            sources_count = conn.execute(text("SELECT COUNT(*) FROM sources")).scalar_one()
            fts_count = conn.execute(text("SELECT COUNT(*) FROM sources_fts")).scalar_one()
        assert sources_count == 3
        assert fts_count == sources_count
    finally:
        engine.dispose()


# ---- Japanese substring match (the whole point of trigram) ----------------


@pytest.mark.parametrize(
    ("body", "query"),
    [
        # The flagship examples from epic #338 §背景 / ADR-0028 §Context.
        ("boxの権限きれてそうなのですが対応お願いします", "boxの権限"),
        ("進捗記入を忘れずに今週の振り返り", "進捗記入"),
        ("CDKのデプロイ周りで質問あります", "CDKの"),
        # Substring in the middle of a longer run-on string — the
        # exact case unicode61 failed on.
        ("週次レビューで依頼内容を確認してください", "依頼内容"),
    ],
)
def test_japanese_substring_query_hits_after_trigram_rebuild(
    head_engine: Engine, body: str, query: str
) -> None:
    """Japanese natural text matches via substring after 0028.

    The MATCH still uses a phrase-quoted literal (the same shape
    ``SearchService._phrase_quote`` produces in non-raw mode) so
    the assertion verifies the tokenizer change alone — no
    SearchService-level fallback is involved (that lives in S3,
    ADR-0028 §Decision (b)).
    """
    with head_engine.begin() as conn:
        _insert_source(conn, body=body, external_id=f"ja-{query}")

    with head_engine.connect() as conn:
        hits = conn.execute(
            text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH :q"),
            {"q": f'"{query}"'},
        ).scalar_one()
    assert hits == 1, f"trigram tokenizer should hit Japanese substring {query!r} inside {body!r}"


# ---- downgrade -----------------------------------------------------------


def test_downgrade_restores_unicode61_tokenizer(tmp_path: Path) -> None:
    """``alembic downgrade`` rolls ``sources_fts`` back to unicode61.

    We upgrade past 0028, then downgrade to 0019 and assert the
    vtable's ``CREATE`` statement names the old tokenizer. This is
    the safety hatch the migration docstring promises for operators
    who want to revert the tokenizer choice.
    """
    db_path = tmp_path / "downgrade.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            row_before = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'sources_fts'")
            ).first()
        assert row_before is not None
        assert "tokenize='trigram'" in row_before.sql
    finally:
        engine.dispose()

    command.downgrade(cfg, _PRE_0028_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            row_after = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'sources_fts'")
            ).first()
            triggers_after = {
                trig.name
                for trig in conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE 'sources_fts%'"
                    )
                )
            }
        assert row_after is not None, "sources_fts must still exist after downgrade to 0019"
        assert "tokenize='unicode61 remove_diacritics 2'" in row_after.sql
        assert "trigram" not in row_after.sql
        # Triggers must be back too — downgrade re-creates them.
        for trigger in _TRIGGER_NAMES:
            assert trigger in triggers_after
    finally:
        engine.dispose()


def test_downgrade_backfills_rows_into_unicode61_index(tmp_path: Path) -> None:
    """Downgrade preserves row coverage: existing bodies land in the rolled-back index.

    Upgrade past 0028, insert a row, downgrade to 0019, and assert
    the unicode61 index contains the row's tokens. This pins the
    downgrade back-fill clause — without it the rolled-back index
    would be empty and operators would silently lose search
    coverage on the way back.
    """
    db_path = tmp_path / "downgrade_backfill.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            _insert_source(
                conn,
                body="round trip body containing rollbackmarker",
                external_id="dgbf-1",
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, _PRE_0028_REVISION)

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            # unicode61 splits on whitespace, so the bare token
            # "rollbackmarker" hits as itself.
            hits = conn.execute(
                text("SELECT COUNT(*) FROM sources_fts WHERE sources_fts MATCH 'rollbackmarker'")
            ).scalar_one()
        assert hits == 1
    finally:
        engine.dispose()


def test_upgrade_then_downgrade_then_upgrade_round_trip_clean(tmp_path: Path) -> None:
    """``upgrade head → downgrade 0019 → upgrade head`` round-trips cleanly.

    Catches accidental ordering or stray-object issues in either
    direction (e.g. forgetting to drop a trigger before
    re-creating it, leaving an orphan FTS shadow table behind).
    The migration must be re-applicable after a downgrade without
    operator intervention.
    """
    db_path = tmp_path / "roundtrip.sqlite"
    cfg = _make_alembic_config(db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, _PRE_0028_REVISION)
    command.upgrade(cfg, "head")

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'sources_fts'")
            ).first()
            triggers = {
                trig.name
                for trig in conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE 'sources_fts%'"
                    )
                )
            }
        assert row is not None
        assert "tokenize='trigram'" in row.sql
        for trigger in _TRIGGER_NAMES:
            assert trigger in triggers
    finally:
        engine.dispose()
