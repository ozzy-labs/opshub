"""Semantic recall service (Phase 4 step C1, ADR-0012 §7).

Hybrid search: vector nearest-neighbor (via
:class:`~opshub.vectors.store.VectorStore`) + SQL filter
(by ``entity_type`` / ``state``) + lookup against the entity tables to
attach human-readable ``title`` / ``state`` metadata. The Phase 4 MVP
CLI (PR C2) renders the returned :class:`RecallHit` list directly; this
service is the only path through which a recall query reaches the
vector store.

The caller wires a :class:`RecallService` with the active
:class:`~opshub.vectors.embedder.Embedder` and
:class:`~opshub.vectors.store.VectorStore` (resolved via
:mod:`opshub.vectors.factory`). The service is stateless beyond those
references — every :meth:`RecallService.recall` call rebuilds the
query.

Model-mismatch handling
-----------------------

:class:`~opshub.vectors.store.VectorStore` routes by embedding dim
(ADR-0012 §1 #5), so a mismatch between the active embedder and the
stored vectors surfaces as either a
:class:`~opshub.core.errors.ConfigError` (when the dim has no matching
vec0 table) or a confused-by-other-backend recall result. We detect
the common case where the configured backend changed since the last
rebuild by checking that at least one ``embeddings`` row exists for
the active ``(model_id, model_version)`` **before** the embedder is
invoked. If zero, we raise :class:`~opshub.core.errors.ConfigError`
asking the operator to run ``opshub embeddings rebuild`` — failing
fast on a config drift avoids wasting an API call on the embedder.

Orphan handling
---------------

A vector hit can outlive its source row (e.g. the operator deleted
an entity but has not yet re-run ``opshub embeddings rebuild`` — the
rebuild path purges orphans on the next pass). The service silently
skips orphan hits so stale embeddings do not surface in the result;
this is not a user-visible error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from opshub.core.errors import ConfigError
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opshub.vectors.embedder import Embedder
    from opshub.vectors.store import VectorStore


__all__ = ["RecallHit", "RecallService"]


# Entity types that do not carry a ``state`` column on their projection
# row (see :mod:`opshub.projections.decisions` / :mod:`opshub.projections.sources`).
# Combining a ``state`` filter with one of these types is a caller
# error rather than an empty result, so the service surfaces it loudly.
_STATELESS_ENTITY_TYPES: frozenset[str] = frozenset({"decision", "source"})


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One hit in a recall result list.

    Attributes
    ----------
    entity_type:
        One of ``"task"`` / ``"decision"`` / ``"inbox_item"`` /
        ``"source"``.
    entity_id:
        ULID of the entity (matches the ``id`` column of the
        corresponding projection table).
    title:
        Human-readable title for display. The service maps each
        entity_type to its display column (``tasks.title`` /
        ``decisions.text`` / ``inbox_items.summary`` / ``sources.title``)
        so the CLI renderer can stay entity-agnostic.
    snippet:
        Longer-form text (Phase 4 MVP: same as title or summary;
        Phase 4.x may extract a query-relevant snippet).
    score:
        Similarity score from the vector store. Implementation-defined
        (cosine similarity for sqlite-vec), ordered descending —
        callers should not assume a particular metric, only that
        "higher is more similar" (see
        :class:`opshub.vectors.store.RecallHit` for the same
        convention).
    """

    entity_type: str
    entity_id: str
    title: str
    snippet: str
    score: float


class RecallService:
    """Hybrid semantic search: vector recall + SQL filter + metadata JOIN.

    Constructor wires an :class:`~opshub.vectors.embedder.Embedder` +
    :class:`~opshub.vectors.store.VectorStore` resolved by
    :mod:`opshub.vectors.factory`. The shared ``engine`` is used for
    every metadata lookup; the service does not open its own
    transactions (every read is a short-lived ``engine.connect()``).
    """

    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        engine: Engine,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._engine = engine

    def recall(
        self,
        query_text: str,
        *,
        entity_type: str | None = None,
        limit: int = 10,
        state: str | None = None,
    ) -> list[RecallHit]:
        """Embed the query text, fetch nearest neighbours, attach metadata.

        Pipeline
        --------

        1. Validate the request (non-empty query, embeddings exist for
           the active model, state filter compatible with entity_type).
        2. Embed the query string via the configured
           :class:`~opshub.vectors.embedder.Embedder`.
        3. Ask the :class:`~opshub.vectors.store.VectorStore` for
           ``max(2*limit, limit)`` nearest hits (the 2x headroom
           absorbs orphan + state-filter drop-outs without a second
           round trip).
        4. For each vector hit, look up the matching projection row
           and attach ``title`` / ``snippet`` / optional ``state``.
        5. Apply the ``state`` filter post-lookup, stop once ``limit``
           hits are collected.

        Parameters
        ----------
        query_text:
            Free-form query. Empty / whitespace-only strings raise
            :class:`~opshub.core.errors.ConfigError`.
        entity_type:
            If set, restrict results to one entity type
            (``"task"`` / ``"decision"`` / ``"inbox_item"`` /
            ``"source"``). ``None`` means "search every type".
        limit:
            Cap on returned hits, applied **after** orphan + state
            filtering. Default ``10``.
        state:
            If set, restrict to entities whose ``state`` column equals
            this value. Only meaningful for entities with a state
            column (``task`` / ``inbox_item``); combining with
            ``"decision"`` / ``"source"`` raises
            :class:`~opshub.core.errors.ConfigError` rather than
            silently returning zero results.

        Raises
        ------
        ConfigError
            When ``query_text`` is empty, when no ``embeddings`` row
            exists for the active ``(model_id, model_version)``
            (operator needs ``opshub embeddings rebuild``), or when
            ``state`` is combined with a stateless ``entity_type``
            (``decision`` / ``source``).
        """
        if not query_text.strip():
            raise ConfigError("recall query text must not be empty")
        if state is not None and entity_type in _STATELESS_ENTITY_TYPES:
            raise ConfigError(
                f"--state filter is not supported for entity_type={entity_type!r} "
                f"(no state column on the projection; stateless entity types are "
                f"{sorted(_STATELESS_ENTITY_TYPES)})"
            )
        # Fail fast on a model mismatch so we never spend an embedder
        # call on a database the operator has not rebuilt yet.
        self._assert_embeddings_exist_for_active_model()

        # 1. Embed the query.
        query_embedding = self._embedder.embed_one(query_text)

        # 2. Vector recall (fetch 2*limit for filter headroom).
        entity_types_filter = [entity_type] if entity_type else None
        vec_hits = self._vector_store.recall(
            query_embedding.vector,
            k=max(limit * 2, limit),
            entity_types=entity_types_filter,
        )

        # 3. Attach metadata + apply state filter.
        hits: list[RecallHit] = []
        for vh in vec_hits:
            if len(hits) >= limit:
                break
            metadata_row = self._fetch_entity_metadata(vh.entity_type, vh.entity_id)
            if metadata_row is None:
                # Entity was deleted but its embedding stayed — skip.
                # The next ``opshub embeddings rebuild`` will purge
                # orphan vectors; this is not a user-visible error.
                continue
            if state is not None and metadata_row.get("state") != state:
                continue
            title_value = metadata_row.get("title") or metadata_row.get("summary") or "(no title)"
            snippet_value = (
                metadata_row.get("snippet")
                or metadata_row.get("title")
                or metadata_row.get("summary")
                or ""
            )
            hits.append(
                RecallHit(
                    entity_type=vh.entity_type,
                    entity_id=vh.entity_id,
                    title=str(title_value),
                    snippet=str(snippet_value),
                    score=vh.score,
                )
            )
        return hits

    # ------------------------------------------------------------------ helpers

    def _assert_embeddings_exist_for_active_model(self) -> None:
        """Raise :class:`ConfigError` if no embeddings exist for active model.

        The ``embeddings`` table is not registered on
        :data:`opshub.db.schema.metadata` (it has no SQLAlchemy
        :class:`~sqlalchemy.Table` declaration — see
        :mod:`opshub.vectors.sqlite_vec_store` for the rationale), so
        the existence check uses :func:`sqlalchemy.text` with bind
        parameters, matching the precedent in
        :mod:`opshub.services.embedding_service`.

        Raising here (before the embedder call) lets the CLI surface a
        clear "run rebuild" message even for paid-per-token backends
        (OpenAI / Voyage) without consuming a request.
        """
        stmt = text(
            "SELECT 1 FROM embeddings WHERE model_id = :mid AND model_version = :mv LIMIT 1"
        ).bindparams(
            mid=self._embedder.model_id,
            mv=self._embedder.model_version,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            raise ConfigError(
                f"no embeddings found for active model "
                f"(model_id={self._embedder.model_id!r}, "
                f"version={self._embedder.model_version!r}). "
                f"Run `opshub embeddings rebuild` after setting "
                f"[embedding] backend in config."
            )

    def _fetch_entity_metadata(self, entity_type: str, entity_id: str) -> dict[str, object] | None:
        """Look up the entity row and return display-relevant fields.

        Returns ``None`` if the entity does not exist (orphan
        embedding — see module docstring). The caller filters those
        out so stale embeddings do not surface in results.

        Per-entity title column convention (mirrors the
        :data:`opshub.services.embedding_service._SOURCES` mapping):

        * ``task`` → :data:`tasks_table` ``title`` (+ ``state``)
        * ``decision`` → :data:`decisions_table` ``text`` (stateless)
        * ``inbox_item`` → :data:`inbox_items_table` ``summary`` (+ ``state``)
        * ``source`` → :data:`sources_table` ``title`` (+ ``summary``,
          stateless)
        """
        if entity_type == "task":
            return self._fetch_one(
                tasks_table,
                entity_id,
                title_col="title",
                state_col="state",
            )
        if entity_type == "decision":
            return self._fetch_one(
                decisions_table,
                entity_id,
                title_col="text",
                state_col=None,
            )
        if entity_type == "inbox_item":
            return self._fetch_one(
                inbox_items_table,
                entity_id,
                title_col="summary",
                state_col="state",
            )
        if entity_type == "source":
            return self._fetch_one(
                sources_table,
                entity_id,
                title_col="title",
                state_col=None,
                snippet_col="summary",
            )
        # Unknown entity_type — treat as orphan so the recall keeps
        # walking. The set of supported types is fixed by Phase 4
        # MVP; a future addition would touch this function and its
        # tests in the same PR.
        return None

    def _fetch_one(
        self,
        table: object,  # SQLAlchemy Table — kept untyped to dodge the public-API import.
        entity_id: str,
        *,
        title_col: str,
        state_col: str | None,
        snippet_col: str | None = None,
    ) -> dict[str, object] | None:
        """Select ``(id, title, [state], [snippet])`` from ``table`` by id.

        Returns ``None`` when no row matches (orphan embedding). The
        ``state`` column is selected only when ``state_col`` is set and
        the column actually exists on the table — keeping the SELECT
        list narrow lets each entity type project only the columns it
        needs.

        The ``snippet_col`` knob covers ``sources`` (which has both
        ``title`` and ``summary``); the recall renderer prefers the
        title for the headline but can fall through to the summary
        for the snippet body.
        """
        # ``table`` is a SQLAlchemy ``Table``; we keep the parameter
        # untyped to avoid a public-API import on the hot path. The
        # tested entry points always pass a registered :class:`Table`
        # from :mod:`opshub.projections`.
        from sqlalchemy import Table  # local import for type-narrowing.

        assert isinstance(table, Table)
        columns = [table.c.id, table.c[title_col]]
        if state_col is not None and state_col in table.c:
            columns.append(table.c[state_col])
        if snippet_col is not None and snippet_col in table.c:
            columns.append(table.c[snippet_col])
        with self._engine.connect() as conn:
            row = conn.execute(select(*columns).where(table.c.id == entity_id)).first()
        if row is None:
            return None
        result: dict[str, object] = {
            "title": getattr(row, title_col, None),
            "summary": getattr(row, title_col, None),  # fallback alias
        }
        if state_col is not None and hasattr(row, state_col):
            result["state"] = getattr(row, state_col)
        if snippet_col is not None and hasattr(row, snippet_col):
            result["snippet"] = getattr(row, snippet_col)
        return result
