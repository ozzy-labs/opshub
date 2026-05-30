"""Tests for :class:`opshub.services.search_service.SearchService`.

Phase 10 step B2 (Sub-issue B, ADR-0012 改訂版 §4 + ADR-0020). The
suite drives the service against a migrated SQLite engine (the same
``migrated_engine`` fixture pattern used by
:mod:`tests.unit.services.test_embedding_service`) so the FTS5
virtual table + triggers from migration ``0019`` are real.

The tests cover:

* Empty / whitespace-only queries surface as :class:`ConfigError`.
* MATCH hits join back to ``sources`` and carry connector / title /
  url / snippet metadata.
* ``connector_name`` filter restricts the result set.
* NULL-body rows (Phase 3-9 / ``box_drive`` historicals) are silently
  absent from MATCH results (the trigger inserts an empty document
  for them so the index stays 1:1 but no MATCH query returns them).
* ``raw_query=True`` opts into FTS5 boolean syntax.
* Default ``raw_query=False`` quotes the input so syntactic
  characters do not accidentally trigger FTS5 syntax (``foo(bar)``
  matches as a literal phrase, not as a parse error).
* Re-observation that flips body (NULL → text, or text A → text B) is
  picked up by the FTS triggers in migration ``0019``.
"""

from __future__ import annotations

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
from opshub.services.search_service import SearchService

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB with ``alembic upgrade head`` applied."""
    db_path = tmp_path / "search_service.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_source(
    engine: Engine,
    *,
    body: str | None,
    summary: str | None = None,
    title: str = "title",
    url: str | None = None,
    connector_name: str = "github",
    source_type: str = "issue",
    external_id: str | None = None,
) -> str:
    """Insert one ``sources`` row through the projection table.

    The migration ``0019`` ``AFTER INSERT`` trigger fires on the
    insert so the FTS5 index picks up ``body`` automatically. Passing
    a unique ``external_id`` per call keeps the
    ``(connector_name, external_id)`` UNIQUE constraint happy when a
    test seeds multiple rows.
    """
    source_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name=connector_name,
                external_id=external_id or f"ext:{source_id}",
                source_type=source_type,
                title=title,
                url=url,
                summary=summary,
                observed_at=now,
                updated_at=now,
                body=body,
            )
        )
    return source_id


# ---- error / edge cases ---------------------------------------------------


def test_empty_query_raises_config_error(migrated_engine: Engine) -> None:
    """An empty / whitespace-only query is a usage error, not an empty hit list."""
    service = SearchService(engine=migrated_engine)
    with pytest.raises(ConfigError, match="must not be empty"):
        service.search("   ")


def test_empty_index_returns_no_hits(migrated_engine: Engine) -> None:
    """No body rows yet → search returns []."""
    service = SearchService(engine=migrated_engine)
    assert service.search("anything") == []


# ---- hit shape -----------------------------------------------------------


def test_matched_body_returns_hit_with_metadata(migrated_engine: Engine) -> None:
    """A body row whose tokens match surfaces with title / url / snippet."""
    seeded_id = _seed_source(
        migrated_engine,
        body="authentication fix landed on develop branch yesterday",
        summary="auth fix",
        title="PR: auth fix",
        url="https://example.com/pr/1",
        connector_name="github",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("authentication")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.entity_id == seeded_id
    assert hit.connector_name == "github"
    assert hit.title == "PR: auth fix"
    assert hit.url == "https://example.com/pr/1"
    assert hit.snippet == "auth fix"
    # Score is the negated bm25() value — positive, "higher is more relevant".
    assert hit.score > 0


def test_null_body_rows_are_never_returned(migrated_engine: Engine) -> None:
    """Phase 3-9 / box_drive NULL-body rows must not pollute results."""
    _seed_source(migrated_engine, body=None, summary="legacy preview")
    matched_id = _seed_source(migrated_engine, body="genuine signal token", summary="match")
    service = SearchService(engine=migrated_engine)

    hits = service.search("signal")

    assert len(hits) == 1
    assert hits[0].entity_id == matched_id


# ---- filter ---------------------------------------------------------------


def test_connector_filter_restricts_results(migrated_engine: Engine) -> None:
    """``connector_name='slack'`` returns Slack hits only."""
    _seed_source(
        migrated_engine,
        body="release notes for the launcher",
        connector_name="github",
        external_id="repo#issue-1",
    )
    slack_id = _seed_source(
        migrated_engine,
        body="release notes thread for the launcher",
        connector_name="slack",
        external_id="C1:msg-1",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("launcher", connector_name="slack")

    assert {h.entity_id for h in hits} == {slack_id}


def test_limit_caps_hit_count(migrated_engine: Engine) -> None:
    """``limit=2`` returns at most 2 hits even if more match."""
    for i in range(5):
        _seed_source(
            migrated_engine,
            body=f"sample release token {i}",
            external_id=f"r{i}",
        )
    service = SearchService(engine=migrated_engine)
    hits = service.search("release", limit=2)
    assert len(hits) == 2


# ---- query quoting --------------------------------------------------------


def test_default_query_is_phrase_quoted_against_fts5_syntax(
    migrated_engine: Engine,
) -> None:
    """A query like ``foo(bar)`` matches its tokens literally, not as syntax.

    Without phrase quoting the parentheses would be parsed by FTS5 as
    grouping and the search would surface a malformed-MATCH error.
    """
    seeded_id = _seed_source(migrated_engine, body="call foo(bar) once")
    service = SearchService(engine=migrated_engine)

    hits = service.search("foo(bar)")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id


def test_raw_query_enables_fts5_boolean_syntax(migrated_engine: Engine) -> None:
    """``raw_query=True`` lets the operator use ``OR`` / ``AND`` / prefix syntax."""
    alpha_id = _seed_source(
        migrated_engine,
        body="alpha release notes",
        external_id="alpha",
    )
    beta_id = _seed_source(
        migrated_engine,
        body="beta release",
        external_id="beta",
    )
    other_id = _seed_source(
        migrated_engine,
        body="completely unrelated",
        external_id="other",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("alpha OR beta", raw_query=True)

    ids = {h.entity_id for h in hits}
    assert alpha_id in ids
    assert beta_id in ids
    assert other_id not in ids


# ---- triggers (sync on update / delete) ----------------------------------


def test_update_of_body_refreshes_fts_index(migrated_engine: Engine) -> None:
    """The ``AFTER UPDATE OF body`` trigger keeps the index aligned.

    A row whose body changes from "alpha" to "beta" must stop
    matching "alpha" and start matching "beta" — without that the
    re-observation path (``ON CONFLICT DO UPDATE`` in
    :class:`SourcesProjection`) would leave a stale tokeniser entry.
    """
    seeded_id = _seed_source(
        migrated_engine,
        body="alpha sentinel marker",
        external_id="upd:1",
    )

    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE sources SET body = :body WHERE id = :id").bindparams(
                body="beta replacement marker",
                id=seeded_id,
            )
        )

    service = SearchService(engine=migrated_engine)
    assert service.search("alpha") == []
    new_hits = service.search("beta")
    assert len(new_hits) == 1
    assert new_hits[0].entity_id == seeded_id


def test_delete_drops_fts_entry(migrated_engine: Engine) -> None:
    """``AFTER DELETE`` removes the inverted-index entry."""
    seeded_id = _seed_source(
        migrated_engine,
        body="ephemeral test token",
        external_id="del:1",
    )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM sources WHERE id = :id").bindparams(id=seeded_id))

    service = SearchService(engine=migrated_engine)
    assert service.search("ephemeral") == []
