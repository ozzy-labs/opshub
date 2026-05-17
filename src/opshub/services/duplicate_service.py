"""Offline duplicate detection for embeddings (Phase 4 step C3).

Scans the active backend's vec0 table for entity pairs whose cosine
similarity exceeds a threshold and returns them as
:class:`DuplicatePair` results. Phase 4 MVP exposes this through
``opshub embeddings find-duplicates``; auto-detection during
``connector sync`` is Phase 4.x.

The detection is intentionally simple: for each embedded entity, the
:class:`~opshub.vectors.store.VectorStore` returns its k nearest
neighbours; pairs with similarity ≥ ``threshold`` are emitted, with
self-matches and reverse-pairs de-duplicated.

sqlite-vec returns L2 distance, which for unit-normalised vectors
relates to cosine similarity via
``cosine_similarity = 1 - L2_distance^2 / 2``. The
:class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore` further
exposes that distance as ``RecallHit.score = -L2_distance`` so the
Protocol's "higher = more similar" invariant holds. Squaring the
score in the conversion (``1 - score^2 / 2``) is sign-stable, so the
formula yields the correct cosine similarity for either sign
convention as long as the underlying vectors are unit-normalised.

ADR-0012 §1 + :mod:`opshub.vectors.local_embedder` (`normalize_embeddings=True`)
keep the local backend normalised; OpenAI / Voyage return
unit-normalised vectors by API contract. The CLI ``--threshold``
flag therefore accepts a cosine *similarity* in ``[0, 1]`` and the
conversion is hidden from operators.

The CLI surface is ``opshub embeddings find-duplicates`` (this PR
adds the subcommand). Neighbour lookup uses
:meth:`~opshub.vectors.store.VectorStore.recall_by_rowid` so the
service never re-embeds source text — the entity's already-stored
vector is fed straight back through vec0's ``MATCH``. The
:class:`~opshub.vectors.embedder.Embedder` is still injected (and
required) because the service needs ``model_id`` /
``model_version`` to scope the EXISTS JOIN to the active backend;
its ``embed_one`` / ``embed`` methods are not invoked on a duplicate
scan.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from opshub.core.errors import ConfigError
from opshub.projections.decisions import decisions_table
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Engine

    from opshub.vectors.embedder import Embedder
    from opshub.vectors.store import VectorStore


__all__ = ["DuplicatePair", "DuplicateService"]


# Per-entity (table, embedded-text column) mapping. The embedded-text
# column must match what :class:`~opshub.services.embedding_service.EmbeddingService`
# fed to the :class:`Embedder` — otherwise re-embedding here would
# produce a query vector unrelated to the stored vectors. The mapping
# below mirrors :data:`opshub.services.embedding_service._SOURCES`:
#
# * tasks       → title
# * decisions   → text
# * inbox_items → summary
# * sources     → summary
#
# ``sources`` is the default scope (`entity_type="source"`) because
# duplicates from connectors are the most common operational concern
# (Phase 4 plan §3 機能 §6).
_ENTITY_TEXT_COLUMNS: dict[str, tuple[str, str]] = {
    "task": ("tasks", "title"),
    "decision": ("decisions", "text"),
    "inbox_item": ("inbox_items", "summary"),
    "source": ("sources", "summary"),
}


# Resolve the SQLAlchemy ``Table`` per entity type so the projection
# read can use static column descriptors instead of metadata lookups
# (matching the pattern in :mod:`opshub.services.embedding_service`).
_TABLE_BY_ENTITY_TYPE: dict[str, Table] = {
    "task": tasks_table,
    "decision": decisions_table,
    "inbox_item": inbox_items_table,
    "source": sources_table,
}


@dataclass(frozen=True, slots=True)
class DuplicatePair:
    """One detected near-duplicate pair.

    Attributes
    ----------
    entity_type:
        Both entities share this type (the scan is single-typed per
        :meth:`DuplicateService.find_duplicates` invocation).
    entity_id_a / entity_id_b:
        ULIDs of the pair, ordered lexicographically ascending so the
        same pair only emits once regardless of which side the scan
        encountered first.
    text_a / text_b:
        Human-readable display strings (the embedded text — task title
        / decision text / inbox summary / source summary). The CLI
        renderer truncates them for terminal output.
    similarity:
        Cosine similarity in ``[0, 1]``; the threshold is the lower
        bound for emission.
    """

    entity_type: str
    entity_id_a: str
    entity_id_b: str
    text_a: str
    text_b: str
    similarity: float


class DuplicateService:
    """Scan embeddings for near-duplicate pairs above a similarity threshold.

    Parameters
    ----------
    embedder:
        Concrete :class:`~opshub.vectors.embedder.Embedder`. Used
        only to identify the "current" ``(model_id, model_version)``
        so the projection JOIN yields exactly the entities embedded
        by the active backend. ``embed_one`` / ``embed`` are not
        invoked — neighbour lookup goes through
        :meth:`VectorStore.recall_by_rowid` instead.
    vector_store:
        Concrete :class:`~opshub.vectors.store.VectorStore` providing
        the ``recall_by_rowid`` Protocol method.
    engine:
        SQLAlchemy :class:`~sqlalchemy.engine.Engine` used to read the
        per-entity projection tables and the ``embeddings`` metadata
        table on every scan.
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

    # ------------------------------------------------------------------ commands

    def find_duplicates(
        self,
        *,
        entity_type: str = "source",
        threshold: float = 0.92,
        limit: int = 100,
        per_entity_neighbors: int = 5,
    ) -> list[DuplicatePair]:
        """Return up to ``limit`` near-duplicate pairs for ``entity_type``.

        For each entity in scope that has a current
        ``(model_id, model_version)`` embedding, ask the
        :class:`VectorStore` for the top ``per_entity_neighbors`` hits
        using :meth:`VectorStore.recall_by_rowid` (no re-embed). Pairs
        whose :func:`_score_to_cosine_similarity` exceeds ``threshold``
        are emitted, with self-matches and reverse-pairs de-duplicated.
        Results are sorted by similarity descending so the operator
        sees the strongest matches first.

        Parameters
        ----------
        entity_type:
            Which entity family to scan. Defaults to ``"source"`` —
            connector-sourced duplicates are the primary operational
            concern (Phase 4 plan §3 機能 §6). Must be one of
            :data:`_ENTITY_TEXT_COLUMNS` keys; unknown values raise
            :class:`~opshub.core.errors.ConfigError`.
        threshold:
            Minimum cosine similarity in ``[0, 1]``. Out-of-range
            values raise :class:`~opshub.core.errors.ConfigError`.
        limit:
            Cap on returned pairs. The scan stops emitting once the
            cap is reached, but every entity is still inspected so
            the sort returns the highest-similarity pairs available
            up to that point.
        per_entity_neighbors:
            How many nearest neighbours to fetch per source entity.
            Higher = more thorough but slower (each call is one
            sqlite-vec MATCH). Default ``5`` covers most practical
            near-duplicates.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ConfigError(f"threshold must be in [0, 1]; got {threshold}")
        if entity_type not in _ENTITY_TEXT_COLUMNS:
            known = ", ".join(sorted(_ENTITY_TEXT_COLUMNS))
            raise ConfigError(
                f"unknown entity_type for duplicate detection: {entity_type!r}"
                f" (expected one of: {known})"
            )

        # Materialise once: subsequent neighbour lookups need
        # ``id → text`` for the display string + an authoritative
        # "which entities have an embedding" set so we never call
        # :meth:`Embedder.embed_one` for an entity the projection JOIN
        # excluded.
        text_lookup = dict(self._iter_embedded_entities(entity_type))

        seen_pairs: set[tuple[str, str]] = set()
        results: list[DuplicatePair] = []
        for source_id, source_text in text_lookup.items():
            if not source_text or not source_text.strip():
                # Embedding service skips empty/whitespace text, so
                # there's no stored vector — but defend in case a
                # caller seeded the projection directly.
                continue
            neighbors = self._fetch_neighbors(
                entity_type=entity_type,
                source_id=source_id,
                k=per_entity_neighbors,
            )
            for neighbor in neighbors:
                if neighbor.entity_id == source_id:
                    continue  # self-match
                pair_key = _ordered_pair(source_id, neighbor.entity_id)
                if pair_key in seen_pairs:
                    continue
                similarity = _score_to_cosine_similarity(neighbor.score)
                if similarity < threshold:
                    continue
                seen_pairs.add(pair_key)
                a_id, b_id = pair_key
                neighbor_text = text_lookup.get(neighbor.entity_id) or ""
                if source_id == a_id:
                    text_a, text_b = source_text, neighbor_text
                else:
                    text_a, text_b = neighbor_text, source_text
                results.append(
                    DuplicatePair(
                        entity_type=entity_type,
                        entity_id_a=a_id,
                        entity_id_b=b_id,
                        text_a=text_a,
                        text_b=text_b,
                        similarity=similarity,
                    )
                )

        # Sort highest-similarity first for operator review, then
        # clamp to the caller's limit.
        results.sort(key=lambda p: p.similarity, reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------ helpers

    def _iter_embedded_entities(self, entity_type: str) -> Iterator[tuple[str, str | None]]:
        """Yield ``(entity_id, text)`` for every entity that has a current embedding.

        Joins the entity's projection table to the ``embeddings``
        metadata table on ``(entity_type, entity_id, model_id,
        model_version)`` so the scan only sees entities embedded by
        the *active* backend. Switching backends in config produces a
        new ``model_id`` and a (typically) empty result on the first
        call until :meth:`EmbeddingService.embed_pending` repopulates
        the projection — that "no current embeddings yet" state is
        the right behaviour (you cannot find duplicates that have not
        yet been computed).

        The ``embeddings`` table is **not** registered on
        :data:`opshub.db.schema.metadata` (matching the
        :class:`SqliteVecStore` / :class:`EmbeddingService`
        precedent), so the filter clause is composed with
        :func:`sqlalchemy.text` and bound parameters.
        """
        source_table = _TABLE_BY_ENTITY_TYPE[entity_type]
        _, text_column = _ENTITY_TEXT_COLUMNS[entity_type]
        id_col = source_table.c["id"]
        text_col = source_table.c[text_column]
        # Mirror :meth:`EmbeddingService._iter_pending` but invert the
        # predicate: we want the entities the active backend HAS
        # embedded, so EXISTS rather than NOT EXISTS.
        table_name = source_table.name
        exists_sql = text(
            "EXISTS (SELECT 1 FROM embeddings "
            "WHERE embeddings.entity_type = :__et "
            f"  AND embeddings.entity_id = {table_name}.id "
            "  AND embeddings.model_id = :__mid "
            "  AND embeddings.model_version = :__mv)"
        ).bindparams(
            __et=entity_type,
            __mid=self._embedder.model_id,
            __mv=self._embedder.model_version,
        )
        stmt = select(id_col, text_col).where(exists_sql)
        with self._engine.connect() as conn:
            for row in conn.execute(stmt):
                yield str(row[0]), (None if row[1] is None else str(row[1]))

    def _fetch_neighbors(
        self,
        *,
        entity_type: str,
        source_id: str,
        k: int,
    ) -> list[_NeighborHit]:
        """Recall the ``k`` nearest entities of ``entity_type`` to ``source_id``.

        Uses :meth:`VectorStore.recall_by_rowid` so the source entity's
        already-stored embedding is reused — no re-embed round-trip.
        Self-match is not filtered by the store; the caller drops it
        when assembling :class:`DuplicatePair` results.
        """
        hits = self._vector_store.recall_by_rowid(
            entity_type,
            source_id,
            k=k,
            entity_types=[entity_type],
        )
        return [_NeighborHit(entity_id=hit.entity_id, score=hit.score) for hit in hits]


@dataclass(frozen=True, slots=True)
class _NeighborHit:
    """Internal projection of :class:`~opshub.vectors.store.RecallHit`.

    The service does not need the ``vector`` payload (cosine
    similarity is computed from the score) so we drop it here to
    keep the inner loop small. ``entity_type`` is also dropped
    because the caller scopes the recall with
    ``entity_types=[entity_type]``.
    """

    entity_id: str
    score: float


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    """Return ``(min, max)`` so reverse-pairs collapse to a single key.

    Using a sorted 2-tuple as the dedup key means scanning entity A
    finding B, and scanning B finding A, both produce the same key
    and the second emission is dropped.
    """
    return (left, right) if left <= right else (right, left)


def _score_to_cosine_similarity(score: float) -> float:
    """Convert :class:`~opshub.vectors.store.RecallHit.score` to cosine similarity.

    :class:`~opshub.vectors.sqlite_vec_store.SqliteVecStore` returns
    ``score = -L2_distance`` so the Protocol's "higher = more
    similar" invariant holds. For unit-normalised vectors
    (ADR-0012 §1; :mod:`opshub.vectors.local_embedder` uses
    ``normalize_embeddings=True``; OpenAI / Voyage normalise by API
    contract), L2 distance and cosine similarity are related by

    ::

        cosine_similarity = 1 - L2_distance^2 / 2

    Squaring is sign-stable so this formula yields the correct
    similarity regardless of whether ``score`` is the raw distance
    or its additive inverse. Out-of-range outputs (floating-point
    noise around the limits or non-normalised vectors) are clamped
    to ``[0, 1]`` so the threshold comparison in
    :meth:`DuplicateService.find_duplicates` is well-defined.
    """
    similarity = 1.0 - (score**2) / 2.0
    return max(0.0, min(1.0, similarity))
