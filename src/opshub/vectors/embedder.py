"""Embedder Protocol — Phase 4 surface, frozen in Phase 1.

A concrete ``Embedder`` (e.g. ``LocalSentenceTransformerEmbedder``,
``OpenAIEmbedder``) lives behind this Protocol and is selected at runtime via
``config.toml`` (ADR-0012). Phase 1 only freezes the shape; no implementation
ships here.

Vectors are exposed as ``tuple[float, ...]`` to keep this module stdlib-only.
Implementations are free to use ``numpy.ndarray`` internally; they must
materialise the boundary value as a Python tuple before handing it over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """One embedding produced by an :class:`Embedder`.

    ``model_id`` + ``model_version`` travel alongside the vector so the vector
    store can record provenance and trigger incremental re-embed when the
    configured model changes (ADR-0012 §5).
    """

    vector: tuple[float, ...]
    model_id: str
    model_version: str
    dim: int


@runtime_checkable
class Embedder(Protocol):
    """Pluggable text-to-vector encoder.

    Implementations report their identity through three properties so callers
    (projector, vector store, CLI ``embeddings status``) can persist provenance
    without depending on the concrete class.
    """

    @property
    def model_id(self) -> str:
        """Stable model identifier (e.g. ``"bge-m3"`` or ``"openai:text-embedding-3-small"``)."""
        ...

    @property
    def model_version(self) -> str:
        """Version tag within ``model_id`` (e.g. ``"v1"`` / ``"2026-05-01"``)."""
        ...

    @property
    def dim(self) -> int:
        """Output vector dimensionality. Must match every ``EmbeddingResult.dim``."""
        ...

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        """Encode a batch of texts. Order of results matches order of inputs."""
        ...

    def embed_one(self, text: str) -> EmbeddingResult:
        """Encode a single text. Convenience wrapper around :meth:`embed`."""
        ...
