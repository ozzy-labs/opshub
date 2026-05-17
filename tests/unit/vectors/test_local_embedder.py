"""Tests for :class:`LocalSentenceTransformerEmbedder` (Phase 4 step A3).

Heavy: ``sentence_transformers`` pulls torch (~500MB-2GB). Skip the entire
module in CI via :func:`pytest.importorskip` so contributors can run the
dev test suite without the ``[local-embedding]`` extras. Local devs who
have the extras installed get full coverage automatically.

The first ``BAAI/bge-m3`` model load downloads ~2.3GB and takes ~10-30s
on a cold cache; we share one model instance across the module via a
``scope="module"`` fixture so subsequent tests reuse it.
"""

from __future__ import annotations

import math
import sys

import pytest

pytest.importorskip("sentence_transformers")

from opshub.core.errors import ConfigError
from opshub.vectors import Embedder, EmbeddingResult
from opshub.vectors.local_embedder import LocalSentenceTransformerEmbedder


@pytest.fixture(scope="module")
def embedder() -> LocalSentenceTransformerEmbedder:
    """Shared embedder fixture; loads ``BAAI/bge-m3`` once per module."""
    instance = LocalSentenceTransformerEmbedder()
    # Force model load up front so the cost is paid in the fixture, not
    # in whichever test happens to embed first.
    instance.embed_one("warmup")
    return instance


# ---- Identity / Protocol conformance --------------------------------------


def test_model_id_version_dim_properties() -> None:
    """Default-constructed embedder exposes the documented identity."""
    instance = LocalSentenceTransformerEmbedder()
    assert instance.model_id == "BAAI/bge-m3"
    assert instance.model_version == "v1"
    assert instance.dim == 1024


def test_satisfies_embedder_protocol() -> None:
    """The class must structurally satisfy the Phase 1 ``Embedder`` Protocol."""
    instance = LocalSentenceTransformerEmbedder()
    assert isinstance(instance, Embedder)


def test_constructor_accepts_overrides() -> None:
    """All constructor knobs are kw-only and round-trip through properties."""
    instance = LocalSentenceTransformerEmbedder(
        model_id="custom/model",
        model_version="v9",
        dim=42,
        batch_size=8,
    )
    assert instance.model_id == "custom/model"
    assert instance.model_version == "v9"
    assert instance.dim == 42


# ---- Behaviour ------------------------------------------------------------


def test_embed_empty_list_returns_empty() -> None:
    """``embed([])`` short-circuits without loading the model.

    Empty inputs must not pay the model-load cost; this also lets callers
    safely fan out batches that may be empty.
    """
    instance = LocalSentenceTransformerEmbedder()
    assert instance.embed([]) == []


def test_embed_one_returns_single_result_with_correct_dim(
    embedder: LocalSentenceTransformerEmbedder,
) -> None:
    """A single text produces one ``EmbeddingResult`` with the declared dim."""
    result = embedder.embed_one("hello world")
    assert isinstance(result, EmbeddingResult)
    assert result.model_id == "BAAI/bge-m3"
    assert result.model_version == "v1"
    assert result.dim == 1024
    assert len(result.vector) == 1024
    assert all(isinstance(x, float) for x in result.vector)


def test_embed_batch_of_3_returns_3_distinct_results(
    embedder: LocalSentenceTransformerEmbedder,
) -> None:
    """Distinct inputs produce distinct vectors in input order."""
    texts = [
        "the quick brown fox jumps over the lazy dog",
        "machine learning models embed text into vectors",
        "ご飯を食べる",
    ]
    results = embedder.embed(texts)
    assert len(results) == 3
    for r in results:
        assert isinstance(r, EmbeddingResult)
        assert r.dim == 1024
        assert len(r.vector) == 1024
    # Inputs are semantically different so no two vectors should be identical.
    assert results[0].vector != results[1].vector
    assert results[1].vector != results[2].vector
    assert results[0].vector != results[2].vector


def test_vectors_are_normalised(embedder: LocalSentenceTransformerEmbedder) -> None:
    """``normalize_embeddings=True`` means every vector has L2 norm ~= 1.0."""
    results = embedder.embed(["alpha", "beta", "gamma"])
    for r in results:
        norm = math.sqrt(sum(x * x for x in r.vector))
        assert math.isclose(norm, 1.0, abs_tol=1e-3), f"L2 norm drifted: {norm!r}"


# ---- Failure modes --------------------------------------------------------


def test_missing_extras_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``sentence_transformers`` cannot be imported, ``ConfigError`` fires.

    Setting the module to ``None`` in :data:`sys.modules` makes subsequent
    ``import sentence_transformers`` raise ``ImportError`` -- this simulates
    the case where a user installed plain ``opshub`` without the
    ``[local-embedding]`` extras and accidentally configured the local
    backend.
    """
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    instance = LocalSentenceTransformerEmbedder()
    with pytest.raises(ConfigError, match="local-embedding"):
        instance.embed(["anything"])
