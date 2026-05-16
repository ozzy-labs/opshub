"""Freeze tests for the Phase 4 vector interface.

These tests assert the *exact* shape of :class:`opshub.vectors.Embedder` and
:class:`opshub.vectors.VectorStore`. If anyone changes a method name, renames
a parameter, alters a type annotation, or flips a kw-only marker, these tests
fail loudly — that is the entire point.

Rationale (ADR-0012 §軽減策 1): config (step 12) and CLI start referencing the
Protocol by name in Phase 1. Allowing it to drift before Phase 4 lands the
concrete implementation would defeat the freeze.
"""

from __future__ import annotations

import inspect
from typing import Protocol, get_type_hints

from opshub.vectors import (
    Embedder,
    EmbeddingResult,
    RecallHit,
    StoredEmbedding,
    VectorStore,
)

# ---- Protocol identity ----------------------------------------------------


def test_embedder_is_runtime_checkable_protocol() -> None:
    assert issubclass(Embedder, Protocol)  # type: ignore[arg-type]
    # runtime_checkable sets this dunder; assert it explicitly so dropping the
    # decorator fails the freeze test.
    assert getattr(Embedder, "_is_runtime_protocol", False) is True


def test_vector_store_is_runtime_checkable_protocol() -> None:
    assert issubclass(VectorStore, Protocol)  # type: ignore[arg-type]
    assert getattr(VectorStore, "_is_runtime_protocol", False) is True


# ---- Embedder surface -----------------------------------------------------


def test_embedder_member_names_are_frozen() -> None:
    expected = {"model_id", "model_version", "dim", "embed", "embed_one"}
    actual = {name for name in vars(Embedder) if not name.startswith("_")}
    assert actual == expected, f"Embedder surface drifted. expected={expected!r} actual={actual!r}"


def test_embedder_properties_are_properties() -> None:
    for name in ("model_id", "model_version", "dim"):
        attr = inspect.getattr_static(Embedder, name)
        assert isinstance(attr, property), f"{name!r} must be a property on Embedder"


def test_embedder_property_return_types() -> None:
    # Property fget annotations capture the declared return types.
    model_id_fget = inspect.getattr_static(Embedder, "model_id").fget
    model_version_fget = inspect.getattr_static(Embedder, "model_version").fget
    dim_fget = inspect.getattr_static(Embedder, "dim").fget
    assert model_id_fget is not None
    assert model_version_fget is not None
    assert dim_fget is not None
    assert get_type_hints(model_id_fget)["return"] is str
    assert get_type_hints(model_version_fget)["return"] is str
    assert get_type_hints(dim_fget)["return"] is int


def test_embedder_embed_signature() -> None:
    sig = inspect.signature(Embedder.embed)
    assert list(sig.parameters) == ["self", "texts"]
    hints = get_type_hints(Embedder.embed)
    assert hints["texts"] == list[str]
    assert hints["return"] == list[EmbeddingResult]


def test_embedder_embed_one_signature() -> None:
    sig = inspect.signature(Embedder.embed_one)
    assert list(sig.parameters) == ["self", "text"]
    hints = get_type_hints(Embedder.embed_one)
    assert hints["text"] is str
    assert hints["return"] is EmbeddingResult


# ---- VectorStore surface --------------------------------------------------


def test_vector_store_member_names_are_frozen() -> None:
    expected = {"upsert", "recall", "count", "delete"}
    actual = {name for name in vars(VectorStore) if not name.startswith("_")}
    assert actual == expected, (
        f"VectorStore surface drifted. expected={expected!r} actual={actual!r}"
    )


def test_vector_store_upsert_signature() -> None:
    sig = inspect.signature(VectorStore.upsert)
    assert list(sig.parameters) == ["self", "embeddings"]
    hints = get_type_hints(VectorStore.upsert)
    assert hints["embeddings"] == list[StoredEmbedding]
    assert hints["return"] is type(None)


def test_vector_store_recall_signature() -> None:
    sig = inspect.signature(VectorStore.recall)
    params = sig.parameters
    assert list(params) == ["self", "query", "k", "entity_types"]
    # k and entity_types must be keyword-only so positional reordering can't
    # silently break callers when we add filters later.
    assert params["k"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["entity_types"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["entity_types"].default is None
    hints = get_type_hints(VectorStore.recall)
    assert hints["query"] == tuple[float, ...]
    assert hints["k"] is int
    assert hints["entity_types"] == list[str] | None
    assert hints["return"] == list[RecallHit]


def test_vector_store_count_signature() -> None:
    sig = inspect.signature(VectorStore.count)
    params = sig.parameters
    assert list(params) == ["self", "entity_type"]
    assert params["entity_type"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["entity_type"].default is None
    hints = get_type_hints(VectorStore.count)
    assert hints["entity_type"] == str | None
    assert hints["return"] is int


def test_vector_store_delete_signature() -> None:
    sig = inspect.signature(VectorStore.delete)
    params = sig.parameters
    assert list(params) == ["self", "entity_type", "entity_id"]
    assert params["entity_type"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["entity_id"].kind is inspect.Parameter.KEYWORD_ONLY
    hints = get_type_hints(VectorStore.delete)
    assert hints["entity_type"] is str
    assert hints["entity_id"] is str
    assert hints["return"] is int


# ---- Runtime checkability with a stdlib-only fake -------------------------


def test_embedder_runtime_check_accepts_duck_typed_fake() -> None:
    """`@runtime_checkable` should accept a structurally-conforming fake."""

    class _FakeEmbedder:
        model_id = "fake"
        model_version = "v0"
        dim = 3

        def embed(self, texts: list[str]) -> list[EmbeddingResult]:
            return [
                EmbeddingResult(
                    vector=(0.0, 0.0, 0.0),
                    model_id=self.model_id,
                    model_version=self.model_version,
                    dim=self.dim,
                )
                for _ in texts
            ]

        def embed_one(self, text: str) -> EmbeddingResult:
            return self.embed([text])[0]

    assert isinstance(_FakeEmbedder(), Embedder)
