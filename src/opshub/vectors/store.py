"""VectorStore Protocol — Phase 4 surface, frozen in Phase 1.

The concrete implementation in Phase 4 will be a ``sqlite-vec`` backed store
(ADR-0012), but the Protocol stays stdlib-only so config / CLI / tests can
reference it without pulling in ``sqlite-vec`` or ``numpy``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StoredEmbedding:
    """A vector ready to be persisted.

    ``created_at`` must be tz-aware (see ``opshub.core.time``). ``entity_type``
    / ``entity_id`` reference the source projection row; the store joins
    against it for hybrid (vector + SQL) recall.
    """

    entity_type: str
    entity_id: str
    model_id: str
    model_version: str
    vector: tuple[float, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecallHit:
    """A single result row returned by :meth:`VectorStore.recall`.

    ``score`` is implementation-defined (cosine similarity for sqlite-vec) and
    ordered descending — callers should not assume a particular metric, only
    that "higher is more similar".
    """

    entity_type: str
    entity_id: str
    score: float
    vector: tuple[float, ...]


@runtime_checkable
class VectorStore(Protocol):
    """Persistence + nearest-neighbour search over embeddings."""

    def upsert(self, embeddings: list[StoredEmbedding]) -> None:
        """Insert or replace embeddings.

        Keyed by ``(entity_type, entity_id, model_id, model_version)`` so a
        single entity can hold multiple vectors when migrating between models.
        """
        ...

    def recall(
        self,
        query: tuple[float, ...],
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:
        """Return the ``k`` nearest hits to ``query``.

        ``entity_types=None`` means "search across all entity types"; passing a
        list narrows the scan to those types (Phase 4 hybrid search hook).
        """
        ...

    def recall_by_rowid(
        self,
        entity_type: str,
        entity_id: str,
        *,
        k: int,
        entity_types: list[str] | None = None,
    ) -> list[RecallHit]:
        """Return ``k`` nearest hits to the stored vector at ``(entity_type, entity_id)``.

        Equivalent to fetching the entity's already-stored embedding and feeding it back to
        :meth:`recall` — but without an extra ``Embedder`` round-trip. Callers that want
        nearest neighbours of an entity that has already been embedded (e.g. offline
        duplicate detection) should prefer this over re-embedding the source text, especially
        for API-backed embedders where each ``embed_one`` is a network call.

        If the entity has no stored embedding the result is empty (consistent with "no hits"
        rather than raising). Self-match (the same ``entity_id``) is **not** filtered — the
        caller decides whether to drop it. When multiple embeddings exist for the same
        entity (e.g. across ``model_id`` / ``model_version``), implementations choose the
        most recently inserted row.
        """
        ...

    def count(self, *, entity_type: str | None = None) -> int:
        """Return number of stored embeddings, optionally filtered by ``entity_type``."""
        ...

    def delete(self, *, entity_type: str, entity_id: str) -> int:
        """Delete all embeddings for ``(entity_type, entity_id)``.

        Returns the number of rows deleted so callers can detect no-op deletes
        (e.g. when an entity has not been embedded yet).
        """
        ...
