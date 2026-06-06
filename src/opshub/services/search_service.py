"""Full-text search service over the ``sources_fts`` virtual table.

Phase 10 step B2 (Sub-issue B, ADR-0012 改訂版 §4 + ADR-0020) ships
hybrid search across the assistant's body store. Vector recall
(:class:`~opshub.services.recall_service.RecallService`) handles
paraphrase / semantic neighbours; this service handles exact-token
queries (function names, channel IDs, URLs, ticket strings) by going
through the SQLite FTS5 index created in migration ``0019``.

The service is intentionally narrow: one method
:meth:`SearchService.search` runs a MATCH query against
``sources_fts`` and joins back to the ``sources`` projection so the
result list carries human-readable metadata
(``title`` / ``url`` / ``summary`` / ``snippet``).

Why a separate service from :class:`RecallService`
---------------------------------------------------

The two paths embed different semantics. :class:`RecallService` runs
``EMBED → vec0 MATCH → projection JOIN`` against ``task`` / ``decision``
/ ``inbox_item`` / ``source``. :class:`SearchService` runs ``FTS5
MATCH → sources JOIN`` and is **source-only** (sources is the only
projection that carries a ``body`` column). Combining them in one
service would conflate two different result schemas (``score`` on the
vector side vs ``bm25 rank`` on the FTS side, supported entity types
differing) and force the caller to branch on internal state. ADR-0012
改訂版 §7 + Phase 10 plan §3-B keep them as two surfaces wired
independently into the new ``opshub search`` CLI and any future MCP /
Skill tools.

Connector-agnostic
------------------

The Phase 10 plan calls for cross-connector hits (Slack / Box /
GitHub / Office). Because the FTS index is over ``sources.body`` and
every connector writes through :class:`SourceObserved`, the
connector identity drops out at the FTS layer — a single MATCH query
returns hits across every connector that populated body. Callers can
optionally restrict by ``connector_name`` (e.g. for "search only
Slack").

Body retention contract
-----------------------

epic #470 / issue #481 promoted :attr:`SourceObserved.body` to required
+ non-empty and :data:`opshub.projections.sources.sources_table`'s
``body`` column to ``NOT NULL`` (migration
``0030_enforce_sources_body_not_null``). Every ``sources`` row therefore
carries a non-empty body — stat-only / metadata-only connectors
substitute ``body = summary`` (ADR-0010 §不変条件 metadata-only rule).
The FTS5 index over ``sources.body`` is dense (one searchable document
per row) and the read path queries ``body`` directly without a
``COALESCE`` fallback. The previous Phase 3-9 ``NULL``-body shim is gone
(ADR-0020 §(d) supersedes).

Query syntax
------------

Bracket the operator-supplied query in FTS5's column-prefixed quote
syntax so the input is treated as a phrase rather than as the
operator's mini-language. This avoids accidental escalation when an
operator pastes a function name like ``foo(bar)`` — the parentheses
would otherwise be parsed as FTS5 grouping. Operators who actually
want boolean operators can opt into raw syntax via the
``raw_query=True`` knob.

Short-query LIKE fallback (Phase 15 S3, ADR-0028 §Decision (b))
---------------------------------------------------------------

The ``sources_fts`` tokenizer is ``trigram`` (Phase 15 S2, migration
``0028``). trigram tokenisation builds the inverted index from 3-char
substrings, which means **inputs shorter than 3 characters never hit
the index**. For operator UX continuity we route 1-2 character
queries through a ``LOWER(body) LIKE LOWER(?)`` full scan instead so
"PR", "依頼", "Q4" surface results consistent with the rest of the
service. The threshold (≤2) is fixed: a 3-char query produces
exactly one trigram which the FTS5 path handles natively, so the
fallback is reserved for the cases the FTS5 path provably cannot
serve.

The fallback is bypassed when ``raw_query=True`` — operators who
opt into raw FTS5 syntax own the meaning of their query string,
including the fact that 1-2 char inputs will produce ``0 hits``.
Mirroring the fallback into raw mode would silently rewrite the
operator's MATCH expression, which is the opposite of the contract
``--raw`` advertises.

Case insensitivity matches the trigram default (case-insensitive at
the ASCII level via ``LOWER()`` on both sides). Japanese characters
have no case concept so the ``LOWER`` is a no-op for them. The query
is NFC-normalised before scanning so an operator pasting a
canonically-equivalent but byte-different sequence (composed vs
decomposed) hits the same rows trigram would.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


__all__ = ["SearchHit", "SearchService"]


# bm25() returns a negative score where smaller (more negative) is more
# relevant. We negate it so the caller sees a positive score where
# "higher is more relevant", matching the convention :class:`RecallHit`
# already established.
_BM25_FLIP = "-bm25(sources_fts)"

# The trigram tokenizer (Phase 15 S2, migration 0028) indexes 3-char
# substrings. Inputs shorter than this threshold never match against
# the FTS5 index, so the service routes them through the LIKE
# fallback instead. See ADR-0028 §Decision (b) for the rationale and
# the rejected alternatives (auto-prefix, dual index, etc).
_MIN_FTS_QUERY_CHARS = 3

# Fixed score for LIKE fallback hits. BM25 is undefined off the FTS
# path so we return a constant. The CLI sorts by ``observed_at DESC``
# inside the SQL, not by score, so the constant doesn't perturb
# ordering — it only keeps the result shape symmetric with the FTS
# branch (every :class:`SearchHit` carries a numeric score).
_LIKE_FALLBACK_SCORE = 1.0


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One full-text search hit.

    Attributes
    ----------
    entity_id:
        ULID of the matching ``sources`` row.
    connector_name:
        Connector that observed the source (``github`` / ``slack`` /
        ``ms365`` / ``box`` / ``box_drive``). Surfaced so callers can
        render a connector badge or filter post-hoc.
    source_type:
        Discriminator of the source kind (``slack_message`` /
        ``ms365_outlook`` / ``box_event`` / etc.). Set by the
        connector that observed the source.
    title:
        Human-readable title from the ``sources.title`` column.
    url:
        Optional URL pointing at the source (``sources.url``). May be
        ``None`` for FS-backed connectors.
    snippet:
        Short snippet drawn from ``sources.summary`` for display.
        FTS5 also exposes a ``snippet()`` aux function that returns a
        highlighted excerpt; we keep that as a Phase 10.x enhancement
        and use summary for the MVP so the CLI stays renderer-
        agnostic.
    score:
        Negated bm25 score (positive, higher = more relevant). The
        absolute magnitude is BM25-defined and not directly
        comparable across queries.
    """

    entity_id: str
    connector_name: str
    source_type: str
    title: str
    url: str | None
    snippet: str
    score: float


class SearchService:
    """Run FTS5 MATCH queries over ``sources_fts`` + join back to ``sources``.

    Constructor takes just the engine — there is no embedder or
    vector store involved. The service is stateless beyond that
    engine reference; every :meth:`search` call opens a short-lived
    connection.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def search(
        self,
        query_text: str,
        *,
        limit: int = 10,
        connector_name: str | None = None,
        raw_query: bool = False,
    ) -> list[SearchHit]:
        """Run an FTS5 MATCH and join hits back to ``sources``.

        Parameters
        ----------
        query_text:
            Operator-supplied query. Phrase-quoted by default so the
            input is treated literally (``foo(bar)`` matches the
            tokens ``foo`` + ``bar`` next to each other). Pass
            ``raw_query=True`` to opt into FTS5's boolean / column /
            prefix syntax (``foo AND bar``, ``"exact phrase"``,
            ``ticket-1234*``).
        limit:
            Maximum number of hits returned, ordered by descending
            relevance.
        connector_name:
            If set, restrict to hits whose ``sources.connector_name``
            equals this value. Operators reach this for "search only
            Slack" / "search only Box".
        raw_query:
            When True, hand ``query_text`` straight to FTS5 with no
            wrapping. Default False quotes the query so the operator
            does not accidentally trip FTS5 syntax characters.

        Raises
        ------
        ConfigError
            When ``query_text`` is empty / whitespace-only — FTS5
            would otherwise raise an opaque ``malformed MATCH
            expression`` that is hostile to a CLI user.

        Returns
        -------
        list[SearchHit]
            Hits ordered by ``-bm25(sources_fts)`` descending on the
            FTS path, or by ``observed_at`` descending on the LIKE
            fallback path (BM25 is undefined off the FTS path so the
            constant ``_LIKE_FALLBACK_SCORE`` is returned for those
            hits). Empty list when there are no matches.
        """
        if not query_text.strip():
            raise ConfigError("search query text must not be empty")

        # Short-query LIKE fallback (Phase 15 S3, ADR-0028 §Decision
        # (b)). trigram tokenisation does not index < 3-char inputs,
        # so route 1-2 char queries through ``LOWER(body) LIKE
        # LOWER(?)`` to keep operator UX continuous. Skip the
        # fallback for ``raw_query=True`` — that contract gives the
        # operator full FTS5 authority and a silent rewrite would
        # break it (see ADR-0028 §Decision (b)). NFC-normalise the
        # query first so a composed / decomposed paste hits the same
        # rows the trigram path would.
        normalised_query = unicodedata.normalize("NFC", query_text).strip()
        if not raw_query and len(normalised_query) < _MIN_FTS_QUERY_CHARS:
            return self._search_like_fallback(
                normalised_query,
                limit=limit,
                connector_name=connector_name,
            )

        match_expr = query_text if raw_query else _phrase_quote(query_text)

        # Compose the MATCH query against the FTS5 virtual table, then
        # join back to ``sources`` for display metadata. Filtering by
        # ``connector_name`` happens on the joined side because the
        # FTS index does not store that column.
        where_clauses = ["sources_fts MATCH :q"]
        params: dict[str, object] = {"q": match_expr, "limit": limit}
        if connector_name is not None:
            where_clauses.append("sources.connector_name = :connector_name")
            params["connector_name"] = connector_name

        sql = (
            "SELECT sources.id AS id, "
            "       sources.connector_name AS connector_name, "
            "       sources.source_type AS source_type, "
            "       sources.title AS title, "
            "       sources.url AS url, "
            "       sources.summary AS summary, "
            f"       ({_BM25_FLIP}) AS score "
            "  FROM sources_fts "
            "  JOIN sources ON sources.rowid = sources_fts.rowid "
            f" WHERE {' AND '.join(where_clauses)} "
            f" ORDER BY {_BM25_FLIP} DESC "
            " LIMIT :limit"
        )

        with self._engine.connect() as conn:
            rows = conn.execute(text(sql).bindparams(**params)).all()

        hits: list[SearchHit] = []
        for row in rows:
            summary_value = row.summary or ""
            hits.append(
                SearchHit(
                    entity_id=str(row.id),
                    connector_name=str(row.connector_name),
                    source_type=str(row.source_type),
                    title=str(row.title),
                    url=(None if row.url is None else str(row.url)),
                    snippet=str(summary_value),
                    score=float(row.score),
                )
            )
        return hits

    def _search_like_fallback(
        self,
        normalised_query: str,
        *,
        limit: int,
        connector_name: str | None,
    ) -> list[SearchHit]:
        """Run a substring scan for short queries (≤ 2 chars).

        Phase 15 S3 / ADR-0028 §Decision (b). trigram does not index
        1-2 character inputs so the FTS5 path would return an empty
        list. We instead scan ``sources.body`` with ``LOWER(body)
        LIKE LOWER(?)``, applying the same ``connector_name`` filter
        and ``limit`` the FTS path applies. Ordering is by
        ``observed_at DESC`` (the FTS path's BM25 is undefined here)
        so the operator sees the freshest matches first.

        SQL injection defence relies on the SQLAlchemy parametrised
        binding (the query and the wrapping ``%`` are bound as a
        single parameter, never concatenated into the SQL string).
        LIKE wildcard escaping (``%`` / ``_`` / ``\\`` in the user's
        query) is applied via an explicit ``ESCAPE`` clause so an
        operator searching for ``50%`` finds the literal ``50%``
        rather than every 5-prefixed token in the corpus.
        """
        # Escape LIKE wildcards in the query side so operator-typed
        # ``%`` / ``_`` / ``\`` are matched literally, not as
        # wildcards. The escape character itself (``\``) is escaped
        # first to avoid double-escaping the user's other characters.
        escaped = normalised_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"

        where_clauses = ["LOWER(sources.body) LIKE LOWER(:pattern) ESCAPE '\\'"]
        params: dict[str, object] = {"pattern": like_pattern, "limit": limit}
        if connector_name is not None:
            where_clauses.append("sources.connector_name = :connector_name")
            params["connector_name"] = connector_name

        # epic #470 / issue #481 promoted ``sources.body`` to NOT NULL
        # (migration 0030); no NULL-body guard is needed. Every row has
        # a non-empty body (metadata-only connectors emit
        # ``body = summary`` to satisfy the invariant, ADR-0010
        # §不変条件).

        sql = (
            "SELECT sources.id AS id, "
            "       sources.connector_name AS connector_name, "
            "       sources.source_type AS source_type, "
            "       sources.title AS title, "
            "       sources.url AS url, "
            "       sources.summary AS summary "
            "  FROM sources "
            f" WHERE {' AND '.join(where_clauses)} "
            " ORDER BY sources.observed_at DESC "
            " LIMIT :limit"
        )

        with self._engine.connect() as conn:
            rows = conn.execute(text(sql).bindparams(**params)).all()

        hits: list[SearchHit] = []
        for row in rows:
            summary_value = row.summary or ""
            hits.append(
                SearchHit(
                    entity_id=str(row.id),
                    connector_name=str(row.connector_name),
                    source_type=str(row.source_type),
                    title=str(row.title),
                    url=(None if row.url is None else str(row.url)),
                    snippet=str(summary_value),
                    score=_LIKE_FALLBACK_SCORE,
                )
            )
        return hits


def _phrase_quote(query_text: str) -> str:
    """Wrap ``query_text`` so FTS5 treats it as a literal phrase.

    FTS5 quote rule: a phrase inside double quotes treats every
    character as literal text, with two consecutive double quotes
    escaping a single embedded one (``""``). We strip the trailing
    whitespace first so a stray newline does not produce a degenerate
    phrase (the FTS5 parser is whitespace-tolerant inside phrases but
    leading / trailing whitespace can throw off bm25 weighting).
    """
    normalised = query_text.strip()
    escaped = normalised.replace('"', '""')
    return f'"{escaped}"'
