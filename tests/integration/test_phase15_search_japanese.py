"""End-to-end integration tests for Phase 15 S3 Japanese search.

Pin the operator-flagged cases from epic #338 §背景 (the table that
motivated the Phase 15 work) against a fully-migrated SQLite database
plus the real :class:`~opshub.services.search_service.SearchService`.
The combined coverage proves the trigram tokenizer (migration
``0028``) and the SearchService LIKE fallback land together as a
coherent operator UX:

* ``boxの権限`` (3+ char run-on Japanese) → trigram path
* ``進捗記入`` (4-char run-on Japanese)   → trigram path
* ``依頼``     (2-char Japanese)         → LIKE fallback
* ``box 権限`` (space-split Japanese)    → trigram path

The unit suite in :mod:`tests.unit.services.test_search_service`
already pins the service contract piece-by-piece. This file is the
end-to-end cross-check that the migration head + the service path
agree on the result set the operator will actually see when they
type the queries in.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert
from sqlalchemy.engine import Engine

from opshub.core.ids import new_ulid
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.sources import sources_table
from opshub.services.search_service import SearchService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


def _make_alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def head_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provision a fresh SQLite DB at ``alembic upgrade head`` (past 0028)."""
    db_path = tmp_path / "phase15_search_japanese.sqlite"
    cfg = _make_alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine_for_sqlite(db_path)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed(
    engine: Engine,
    *,
    body: str,
    connector_name: str = "slack",
    source_type: str = "slack_message",
    external_id: str,
    title: str = "fixture",
) -> str:
    """Insert one ``sources`` row through the projection table.

    The migration ``0028`` ``AFTER INSERT`` trigger fires on the
    insert so the rebuilt trigram FTS index picks up ``body``
    automatically. Returns the generated source id for assertion.
    """
    source_id = new_ulid()
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name=connector_name,
                external_id=external_id,
                source_type=source_type,
                title=title,
                url=None,
                summary=body[:200],
                observed_at=now,
                updated_at=now,
                body=body,
            )
        )
    return source_id


# ---- epic #338 §背景 cases: trigram path ----------------------------------


def test_box_no_kengen_hits_trigram_path(head_engine: Engine) -> None:
    """``boxの権限`` hits a run-on Japanese sentence via trigram substring.

    The operator-witnessed case from epic #338 §背景 first row.
    Under the Phase 10 ``unicode61 remove_diacritics 2`` tokenizer
    this returned 0 hits because the entire Japanese run-on collapsed
    to one token. Phase 15 S2 (trigram) + S3 (this test) prove the
    new path lands.
    """
    seeded_id = _seed(
        head_engine,
        body="boxの権限きれてそうなのですが対応お願いします",
        external_id="e2e:box-perm",
    )
    service = SearchService(engine=head_engine)

    hits = service.search("boxの権限", connector_name="slack")

    assert {h.entity_id for h in hits} == {seeded_id}


def test_shinchoku_kinyuu_hits_trigram_path(head_engine: Engine) -> None:
    """``進捗記入`` (4 chars, middle of a longer sentence) hits via trigram."""
    seeded_id = _seed(
        head_engine,
        body="進捗記入を忘れずに今週の振り返り",
        external_id="e2e:shinchoku",
    )
    service = SearchService(engine=head_engine)

    hits = service.search("進捗記入")

    assert {h.entity_id for h in hits} == {seeded_id}


def test_box_kuhaku_kengen_hits_when_body_contains_phrase(
    head_engine: Engine,
) -> None:
    """``box 権限`` (with embedded space) hits a body containing the phrase.

    Default phrase-quoting wraps the literal ``"box 権限"`` so the
    trigram path matches bodies containing the same fragment with
    the space preserved. The epic §背景 row that motivated this
    case noted operator-witnessed hits under the old unicode61
    tokenizer; the new trigram tokenizer pins the same shape via
    substring rather than via whitespace token split, but still
    requires the body to contain the literal space-separated
    phrase. A run-on body without the space (``boxの権限``) takes
    the dedicated test above.
    """
    seeded_id = _seed(
        head_engine,
        body="please check box 権限 status before merging",
        external_id="e2e:box-space",
    )
    service = SearchService(engine=head_engine)

    hits = service.search("box 権限")

    assert seeded_id in {h.entity_id for h in hits}


# ---- epic #338 §背景 cases: LIKE fallback path ----------------------------


def test_irai_two_char_hits_like_fallback(head_engine: Engine) -> None:
    """``依頼`` (2 chars) hits via SearchService LIKE fallback (S3).

    trigram does not index ≤ 2 char inputs (the entire reason the
    fallback exists, ADR-0028 §Decision (b)). The integration test
    proves the service end-to-end against a head-migrated DB still
    surfaces the row a 2-char operator query targets.
    """
    seeded_id = _seed(
        head_engine,
        body="週次レビューで依頼内容を確認してください",
        external_id="e2e:irai",
    )
    service = SearchService(engine=head_engine)

    hits = service.search("依頼")

    assert {h.entity_id for h in hits} == {seeded_id}
    # The fallback path returns the constant ``1.0`` sentinel score
    # — the FTS5 BM25 score is undefined for inputs shorter than the
    # trigram threshold.
    assert hits[0].score == 1.0


def test_raw_mode_short_query_yields_no_hits_end_to_end(head_engine: Engine) -> None:
    """``--raw`` + 2 chars stays on the FTS path and returns 0 hits.

    Pins ADR-0028 §Decision (b) at the integration boundary: a
    regression that mirrored the LIKE fallback into raw mode would
    silently rewrite the operator's MATCH expression. The operator
    contract for ``--raw`` is "you own the FTS5 syntax", including
    the fact that trigram cannot match 1-2 char inputs.
    """
    _seed(
        head_engine,
        body="依頼内容を確認してください",
        external_id="e2e:irai-raw",
    )
    service = SearchService(engine=head_engine)

    raw_hits = service.search("依頼", raw_query=True)

    assert raw_hits == []
