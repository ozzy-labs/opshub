"""Full-text search service over the ``sources_fts`` virtual table.

Phase 10 step B2 (Sub-issue B, ADR-0012 改訂版 §4 + ADR-0020) ships
hybrid search across the secretary's body store. Vector recall
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

NULL body rows
--------------

Phase 3-9 historic ``sources`` rows and the ``box_drive`` connector
(ADR-0019 §不変条件 (b)) land with ``body = NULL``. The migration
``0019`` trigger inserts an empty FTS document for those rows so the
index stays 1:1 with sources rowids; empty documents are returned by
no MATCH query, so they cannot pollute the result set. This matches
the embedding-side ``COALESCE(body, summary)`` fallback (ADR-0012
改訂版 §4) — FTS only finds rows the connector enriched with full
body, recall finds the rest.

Query syntax
------------

Bracket the operator-supplied query in FTS5's column-prefixed quote
syntax so the input is treated as a phrase rather than as the
operator's mini-language. This avoids accidental escalation when an
operator pastes a function name like ``foo(bar)`` — the parentheses
would otherwise be parsed as FTS5 grouping. Operators who actually
want boolean operators can opt into raw syntax via the
``raw_query=True`` knob.
"""

from __future__ import annotations

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
            Hits ordered by ``-bm25(sources_fts)`` descending. Empty
            list when there are no matches.
        """
        if not query_text.strip():
            raise ConfigError("search query text must not be empty")

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
