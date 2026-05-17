"""Local sentence-transformers based Embedder (Phase 4 step A3, ADR-0012).

Implements the Phase 1 ``Embedder`` Protocol using
``sentence_transformers.SentenceTransformer`` for fully-local embedding
(no external API, no token, no network). Heavy dep (~500MB-2GB with torch);
isolated under the ``[local-embedding]`` extras per ADR-0001.

The model is lazy-loaded on first ``embed()`` call so importing this
module is cheap (cold-start budget intact). Subsequent calls reuse
the cached model instance.

Default model: ``BAAI/bge-m3`` (multilingual, 1024-dim). Configured
via the ``model_id`` constructor arg so callers can swap to a different
sentence-transformers model without code changes.

Typing note: ``sentence_transformers`` is an optional dependency that is
NOT installed in the ``just ci`` workflow (it pulls torch, ~500MB-2GB).
To keep ``pyright --strict`` / ``mypy --strict`` green without the
extras, the lazy-loaded model is held as ``Any`` and the optional
import is gated by ``# type: ignore`` / ``# pyright: ignore``. The
trade-off is acceptable: the boundary into this class is the typed
``Embedder`` Protocol, and the boundary out is :class:`EmbeddingResult`
-- both stay strictly typed.
"""

from __future__ import annotations

from typing import Any

from opshub.vectors.embedder import EmbeddingResult

__all__ = ["LocalSentenceTransformerEmbedder"]


class LocalSentenceTransformerEmbedder:
    """Embedder backed by a local sentence-transformers model.

    Constructor args:
        model_id: HuggingFace model identifier (default ``"BAAI/bge-m3"``).
        model_version: opaque version string for embedding store
            invalidation. Caller controls when to bump (e.g. on model
            file change). Default ``"v1"``.
        dim: expected output dimension. ``BAAI/bge-m3`` produces 1024.
            Asserted on first ``embed``; mismatch raises
            :class:`opshub.core.errors.ConfigError`.
        batch_size: batch size passed to ``model.encode``. Default 32.
    """

    def __init__(
        self,
        *,
        model_id: str = "BAAI/bge-m3",
        model_version: str = "v1",
        dim: int = 1024,
        batch_size: int = 32,
    ) -> None:
        self._model_id_value = model_id
        self._model_version_value = model_version
        self._dim_value = dim
        self._batch_size = batch_size
        # ``Any`` because ``sentence_transformers`` is an optional extra and
        # may not be importable in the type-check environment. See module
        # docstring for the typing rationale.
        self._model: Any = None

    @property
    def model_id(self) -> str:
        return self._model_id_value

    @property
    def model_version(self) -> str:
        return self._model_version_value

    @property
    def dim(self) -> int:
        return self._dim_value

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        """Embed a batch of texts. Lazy-loads the model on first call."""
        if not texts:
            return []
        model = self._ensure_model_loaded()
        # ``encode`` returns ``numpy.ndarray`` of shape (N, dim) when given
        # a list. The type is ``Any`` here because ``sentence_transformers``
        # is an optional extra and we cannot rely on its type stubs being
        # available in the type-check environment.
        vectors = model.encode(
            texts,
            batch_size=self._batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,  # bge models recommend normalised vectors for cosine
            show_progress_bar=False,
        )
        actual_dim = int(vectors.shape[1]) if len(vectors) > 0 else 0
        if actual_dim != self._dim_value:
            from opshub.core.errors import ConfigError

            raise ConfigError(
                f"LocalSentenceTransformerEmbedder: model {self._model_id_value!r} "
                f"produced dim={actual_dim}, expected {self._dim_value}. "
                f"Pass dim={actual_dim} to the constructor, or switch model_id."
            )
        results: list[EmbeddingResult] = []
        for vec in vectors:
            results.append(
                EmbeddingResult(
                    vector=tuple(float(x) for x in vec),
                    model_id=self._model_id_value,
                    model_version=self._model_version_value,
                    dim=self._dim_value,
                )
            )
        return results

    def embed_one(self, text: str) -> EmbeddingResult:
        """Convenience wrapper for single-text embedding."""
        results = self.embed([text])
        assert len(results) == 1
        return results[0]

    def _ensure_model_loaded(self) -> Any:
        if self._model is None:
            # Function-local import: cold start path never touches
            # sentence_transformers (which pulls torch).
            try:
                import sentence_transformers  # type: ignore[import-not-found, unused-ignore]  # pyright: ignore[reportMissingImports]
            except ImportError as exc:
                from opshub.core.errors import ConfigError

                raise ConfigError(
                    "LocalSentenceTransformerEmbedder requires the "
                    "'local-embedding' extras: "
                    "uv pip install 'opshub[local-embedding]'"
                ) from exc
            # Pyright cannot resolve the symbol type without the stubs,
            # so widen explicitly via ``Any`` before constructing.
            module: Any = sentence_transformers
            self._model = module.SentenceTransformer(self._model_id_value)
        return self._model  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
