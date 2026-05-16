"""Pluggable embedder / vector-store interfaces (Phase 4 surface, frozen now).

This module is intentionally **Protocol-only**: no concrete implementation, no
ML / numerical dependency import. We freeze the surface in Phase 1 so that
config (step 12) and CLI (later steps) can reference `Embedder` / `VectorStore`
by name without dragging `numpy`, `sentence-transformers`, `sqlite-vec` etc.
into core install (ADR-0001, ADR-0012).

Design notes:

- Vectors travel across the boundary as ``tuple[float, ...]`` rather than
  ``numpy.ndarray``. Numeric speedups belong to concrete implementations as an
  internal optimization; the Protocol must stay stdlib-only so any consumer
  (CLI / config / tests) can build a fake without pulling in heavy deps.
- Protocols are ``@runtime_checkable`` so duck-typed fakes pass ``isinstance``
  in the few code paths that need a runtime guard.
- ``vectors/`` may import from ``core/`` only (ADR-0004 dependency direction).
"""

from opshub.vectors.embedder import Embedder, EmbeddingResult
from opshub.vectors.store import RecallHit, StoredEmbedding, VectorStore

__all__ = [
    "Embedder",
    "EmbeddingResult",
    "RecallHit",
    "StoredEmbedding",
    "VectorStore",
]
