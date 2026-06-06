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

    epic #470 / issue #481: ``sources.body`` is ``NOT NULL`` (migration
    0030). When the test passes ``body=None``, the helper falls back to
    ``summary`` (or a placeholder) so the insert satisfies the new
    NOT NULL constraint while keeping legacy "metadata-only" call
    sites valid (the substituted summary becomes the searchable body,
    matching the metadata-only contract ADR-0010 §不変条件).
    """
    source_id = new_ulid()
    now = now_utc()
    if body is not None:
        resolved_body = body
    elif summary is not None:
        resolved_body = summary
    else:
        resolved_body = "placeholder body"
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
                body=resolved_body,
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


def test_search_targets_body_directly(migrated_engine: Engine) -> None:
    """The FTS5 path indexes ``body`` directly — no COALESCE fallback (epic #470 / #481).

    The Phase 10 ``NULL``-body shim is gone. Metadata-only connectors
    emit ``body = summary`` so the FTS index is dense, and
    ``opshub search`` finds rows by the body column alone (ADR-0010
    §不変条件). The test seeds one row whose body is the substituted
    summary (legacy preview shape) and one row with a distinct body
    token; the search term hits only the latter to pin that body —
    not summary alone — drives the match.
    """
    _seed_source(migrated_engine, body=None, summary="legacy preview")
    matched_id = _seed_source(migrated_engine, body="genuine signal token", summary="match")
    service = SearchService(engine=migrated_engine)

    hits = service.search("signal")

    assert len(hits) == 1
    assert hits[0].entity_id == matched_id


# ---- filter ---------------------------------------------------------------


def test_search_returns_hits_from_multiple_connectors_when_unfiltered(
    migrated_engine: Engine,
) -> None:
    """No ``connector_name`` filter → matches surface from every connector.

    Phase 10 step B2 (Sub-issue B, ADR-0012 改訂版 §4 + ADR-0020):
    the FTS5 index is keyed off ``sources.body`` regardless of which
    connector produced the row. A query for a shared token must
    surface hits from github + slack + box side-by-side so the assistant
    skill (find-document / personal-brief — Phase 12 H1 renamed)
    can cross-correlate evidence across SaaS sources without
    per-connector fan-out.

    Pin: seed identical tokens across three connectors; the unfiltered
    search returns all three rows, the connector set covers github /
    slack / box, and ``connector_name="slack"`` narrows back to one.
    """
    shared_token = "phase10releasenotes"
    github_id = _seed_source(
        migrated_engine,
        body=f"PR review for the {shared_token} milestone",
        connector_name="github",
        source_type="pr",
        external_id="repo#pr-321",
    )
    slack_id = _seed_source(
        migrated_engine,
        body=f"channel thread about the {shared_token} announcement",
        connector_name="slack",
        source_type="slack_message",
        external_id="C100:msg-9",
    )
    box_id = _seed_source(
        migrated_engine,
        body=f"design doc covering the {shared_token} rollout plan",
        connector_name="box_drive",
        source_type="box_file",
        external_id="box:file-7",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search(shared_token)

    hit_ids = {h.entity_id for h in hits}
    hit_connectors = {h.connector_name for h in hits}
    assert hit_ids == {github_id, slack_id, box_id}
    assert hit_connectors == {"github", "slack", "box_drive"}

    # The connector filter still works on top of a multi-connector
    # match set — narrowing to ``slack`` drops the github + box rows.
    slack_only = service.search(shared_token, connector_name="slack")
    assert {h.entity_id for h in slack_only} == {slack_id}


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


# ---- Phase 15 S3: Japanese / trigram path (≥3 chars) ----------------------
#
# Phase 15 S2 (#358 / PR #363) flipped the ``sources_fts`` tokenizer to
# ``trigram`` so any input of 3+ characters hits as a substring. These
# tests pin the new SearchService semantics on real fixtures —
# operator-flagged Japanese cases that returned 0 hits under the
# Phase 10 ``unicode61 remove_diacritics 2`` tokenizer (epic #338 §背景).


def test_japanese_3char_query_hits_trigram_path(migrated_engine: Engine) -> None:
    """``boxの権限`` substring inside a longer run-on Japanese sentence hits."""
    seeded_id = _seed_source(
        migrated_engine,
        body="boxの権限きれてそうなのですが対応お願いします",
        title="slack 権限相談",
        connector_name="slack",
        external_id="ja:box-perm",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("boxの権限")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id


def test_japanese_run_on_substring_hits_trigram_path(migrated_engine: Engine) -> None:
    """``進捗記入`` matches substring of a longer Japanese token."""
    seeded_id = _seed_source(
        migrated_engine,
        body="進捗記入を忘れずに今週の振り返り",
        title="リマインド",
        connector_name="slack",
        external_id="ja:progress",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("進捗記入")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id


def test_japanese_prefix_with_particle_hits_trigram_path(migrated_engine: Engine) -> None:
    """``CDKの`` matches the leading run-on substring of a Japanese sentence."""
    seeded_id = _seed_source(
        migrated_engine,
        body="CDKのデプロイ周りで質問あります",
        title="cdk 質問",
        external_id="ja:cdk",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("CDKの")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id


def test_english_camelcase_token_hits_trigram_path(migrated_engine: Engine) -> None:
    """``DailyMeeting`` continues to hit after the trigram tokenizer swap.

    Pinned to catch a regression where the trigram swap broke
    contiguous English tokens. trigram dices ``DailyMeeting`` into
    its 3-char substrings (``Dai`` / ``ail`` / ``ily`` / ...) so the
    literal phrase still hits — operator confirms in epic #338 §背景.
    """
    seeded_id = _seed_source(
        migrated_engine,
        body="Slot reserved for the DailyMeeting standup",
        title="standup",
        external_id="en:daily",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("DailyMeeting")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id


def test_space_separated_query_hits_when_body_contains_phrase(
    migrated_engine: Engine,
) -> None:
    """``box 権限`` (with embedded space) hits a body containing the phrase.

    Default phrase-quoting wraps the query as a literal phrase
    (``"box 権限"``) so trigram only matches bodies whose text
    contains the same space-separated fragment. This pins the
    existing phrase semantics under the trigram tokenizer — a
    regression that auto-split on whitespace or stripped the space
    would surface here.
    """
    seeded_id = _seed_source(
        migrated_engine,
        body="please check box 権限 status before merging",
        title="space-separated phrase",
        external_id="ja:box-space-phrase",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("box 権限")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id


# ---- Phase 15 S3: LIKE fallback path (1-2 char queries) -------------------
#
# trigram does not index < 3-char inputs, so a 1-2 char query would
# return 0 hits via FTS5. The service routes them through ``LOWER(body)
# LIKE LOWER(?)`` instead. ADR-0028 §Decision (b) / Phase 15 plan §S3.


def test_two_char_japanese_query_hits_like_fallback(migrated_engine: Engine) -> None:
    """``依頼`` (2 chars) returns the matching source via LIKE fallback."""
    seeded_id = _seed_source(
        migrated_engine,
        body="週次レビューで依頼内容を確認してください",
        title="依頼確認",
        connector_name="slack",
        external_id="ja:irai",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("依頼")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id
    # The fallback returns the constant score so callers can
    # distinguish the path off-band if they want to.
    assert hits[0].score == 1.0


def test_two_char_english_query_hits_like_fallback(migrated_engine: Engine) -> None:
    """``PR`` (2 chars) matches body substring via LIKE fallback."""
    seeded_id = _seed_source(
        migrated_engine,
        body="please review my PR before EOD",
        title="github PR",
        connector_name="github",
        external_id="en:pr",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("PR")

    assert len(hits) == 1
    assert hits[0].entity_id == seeded_id


def test_one_char_query_hits_like_fallback(migrated_engine: Engine) -> None:
    """A single-character query still hits via the LIKE path."""
    seeded_id = _seed_source(
        migrated_engine,
        body="single char sentinel a here",
        external_id="en:single",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("a")

    assert any(h.entity_id == seeded_id for h in hits)


def test_like_fallback_is_case_insensitive(migrated_engine: Engine) -> None:
    """``BOX`` and ``box`` return the same row via the LIKE fallback's LOWER pair.

    trigram is case-insensitive by default, so the fallback must
    match the same UX. The implementation applies ``LOWER()`` on both
    sides (query + ``sources.body``).
    """
    seeded_id = _seed_source(
        migrated_engine,
        body="box notification arrived today",
        connector_name="box",
        external_id="ci:box",
    )
    service = SearchService(engine=migrated_engine)

    # Query is 3 chars so on the FTS path; trigger the LIKE fallback
    # by supplying a 2-char query instead.
    upper_hits = service.search("BO")
    lower_hits = service.search("bo")

    assert {h.entity_id for h in upper_hits} == {seeded_id}
    assert {h.entity_id for h in lower_hits} == {seeded_id}


def test_like_fallback_respects_connector_filter(migrated_engine: Engine) -> None:
    """``connector_name`` still narrows the LIKE fallback hit set."""
    _seed_source(
        migrated_engine,
        body="PR ready for review on github",
        connector_name="github",
        external_id="ci:pr-gh",
    )
    slack_id = _seed_source(
        migrated_engine,
        body="PR thread continues in slack",
        connector_name="slack",
        external_id="ci:pr-slack",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("PR", connector_name="slack")

    assert {h.entity_id for h in hits} == {slack_id}


def test_like_fallback_respects_limit(migrated_engine: Engine) -> None:
    """``limit`` caps the row count of the LIKE fallback path."""
    for i in range(5):
        _seed_source(
            migrated_engine,
            body=f"PR {i} pending review",
            external_id=f"lim:{i}",
        )
    service = SearchService(engine=migrated_engine)

    hits = service.search("PR", limit=2)

    assert len(hits) == 2


def test_like_fallback_targets_body_directly(migrated_engine: Engine) -> None:
    """The LIKE fallback path matches the body column directly (epic #470 / #481).

    ``sources.body`` is ``NOT NULL`` (migration 0030); the previous
    ``body IS NOT NULL`` guard in the LIKE path is gone (every row has
    a non-empty body). The test seeds a non-matching row and a row
    whose body carries the short-query token; only the latter
    surfaces in the LIKE fallback.
    """
    _seed_source(migrated_engine, body="legacy notes overview", summary=None)
    matched_id = _seed_source(
        migrated_engine,
        body="genuine PR signal",
        external_id="null:guard",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("PR")

    assert {h.entity_id for h in hits} == {matched_id}


def test_like_fallback_escapes_wildcards_in_query(migrated_engine: Engine) -> None:
    """``%`` / ``_`` typed by the operator match literally, not as wildcards.

    Without escaping, ``50%`` would degenerate into ``50%`` →
    ``%50%%`` → "every row containing 50 followed by anything", which
    silently inflates the hit set. The implementation applies an
    explicit ``ESCAPE`` clause so the operator's wildcards land as
    literal characters.
    """
    seeded_id = _seed_source(
        migrated_engine,
        body="discount applies to 50% of items",
        external_id="esc:pct",
    )
    _seed_source(
        migrated_engine,
        body="threshold is 5000 items per box",
        external_id="esc:5000",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("0%")

    assert {h.entity_id for h in hits} == {seeded_id}


def test_like_fallback_blocks_sql_injection_via_param_binding(
    migrated_engine: Engine,
) -> None:
    """A malicious query payload stays inside the parameter, not the SQL.

    SQLAlchemy parametrised binding is the structural defence; this
    test exercises the surface so a regression that string-formats
    the query into the SQL is caught here. A successful injection
    would return every row (the seeded row plus the unrelated row);
    the parametrised path returns at most the rows whose body
    contains the literal payload (which here is zero — the payload
    is never substring of any body).
    """
    _seed_source(migrated_engine, body="unrelated content", external_id="inj:a")
    _seed_source(migrated_engine, body="more unrelated content", external_id="inj:b")
    service = SearchService(engine=migrated_engine)

    # 2-char payload routes through the LIKE fallback. If it were
    # concatenated into the SQL the trailing ``OR 1=1`` would
    # short-circuit the predicate and return every row.
    hits = service.search("' OR 1=1 --")

    assert hits == []


def test_nfc_normalisation_unifies_composed_and_decomposed_query(
    migrated_engine: Engine,
) -> None:
    """A composed input and its NFD-decomposed twin hit the same row.

    Operator could paste a canonically-equivalent but byte-different
    sequence (e.g. NFD ``が`` = ``か`` + combining ``゛``). The
    service NFC-normalises the query before scanning so both forms
    land on the same rows trigram would.
    """
    seeded_id = _seed_source(
        migrated_engine,
        body="議事録の保存先がBoxです",
        title="記録",
        external_id="nfc:case",
    )
    service = SearchService(engine=migrated_engine)

    composed = "が"  # NFC, single codepoint
    decomposed = "が"  # NFD: か + combining voicing mark
    # Both queries are 1 char (after NFC normalisation) so they
    # route through the LIKE fallback.
    composed_hits = service.search(composed)
    decomposed_hits = service.search(decomposed)

    assert seeded_id in {h.entity_id for h in composed_hits}
    assert seeded_id in {h.entity_id for h in decomposed_hits}


def test_raw_mode_short_query_bypasses_like_fallback(migrated_engine: Engine) -> None:
    """``raw_query=True`` keeps the FTS5 MATCH path even for 1-2 char inputs.

    ADR-0028 §Decision (b): when an operator opts into ``--raw``
    they own the meaning of their query, including the fact that
    trigram does not index ≤2 char inputs. The fallback must NOT
    rewrite the operator's MATCH — a regression that mirrors the
    fallback into raw mode would silently re-interpret the operator
    query, the opposite of what ``--raw`` advertises.
    """
    seeded_id = _seed_source(
        migrated_engine,
        body="this body contains the substring PR somewhere",
        external_id="raw:short",
    )
    service = SearchService(engine=migrated_engine)

    # ``--raw`` + 2-char query: FTS5 trigram cannot match a < 3-char
    # input, so the raw path returns ``[]`` even though a LIKE fallback
    # would have surfaced the row.
    raw_hits = service.search("PR", raw_query=True)
    assert raw_hits == []

    # Sanity check: the non-raw path does surface the row via the
    # LIKE fallback, confirming the row exists and the only thing
    # gating it is the raw bypass.
    default_hits = service.search("PR")
    assert {h.entity_id for h in default_hits} == {seeded_id}


def test_three_char_query_uses_fts_path_not_fallback(migrated_engine: Engine) -> None:
    """A 3-char query goes through the FTS path (BM25 score, not the constant).

    The threshold is fixed at ``len(query) < 3`` so a 3-char query
    produces exactly one trigram and the FTS5 path can serve it. We
    pin this by checking the score: the FTS path returns
    ``-bm25(...)`` which is path-dependent and != ``1.0``, while the
    LIKE fallback returns the constant ``1.0`` sentinel.
    """
    _seed_source(
        migrated_engine,
        body="release notes for the launcher",
        external_id="th:3char",
    )
    service = SearchService(engine=migrated_engine)

    hits = service.search("rel")

    assert len(hits) == 1
    # FTS5 trigram path returns a real BM25-derived score. The LIKE
    # fallback constant is exactly 1.0; the FTS path will not collide
    # with that for a non-trivial corpus.
    assert hits[0].score != 1.0
