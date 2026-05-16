"""Value-object guarantees for the vectors module.

The three dataclasses are part of the frozen Phase 4 surface: they are passed
across the Embedder / VectorStore boundary and persisted by the projector.
This file asserts the documented invariants — frozen, slotted, exact field
names + types — so they cannot drift before Phase 4 lands the implementation.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any, get_type_hints

import pytest

from opshub.vectors import EmbeddingResult, RecallHit, StoredEmbedding


def _dc_params(cls: type) -> Any:
    """Return ``cls.__dataclass_params__``.

    Helper isolates the ``Any`` cast in one place so the test bodies stay
    type-clean under pyright strict (``__dataclass_params__`` is a CPython
    implementation detail without a public stub).
    """
    return cls.__dataclass_params__  # type: ignore[attr-defined]


# ---- EmbeddingResult ------------------------------------------------------


def test_embedding_result_is_frozen_and_slotted() -> None:
    params = _dc_params(EmbeddingResult)
    assert params.frozen is True
    assert params.slots is True
    # slots dataclasses do not expose __dict__ on instances.
    instance = EmbeddingResult(vector=(0.1, 0.2), model_id="m", model_version="v", dim=2)
    assert not hasattr(instance, "__dict__")


def test_embedding_result_fields_and_types() -> None:
    expected = {
        "vector": tuple[float, ...],
        "model_id": str,
        "model_version": str,
        "dim": int,
    }
    hints = get_type_hints(EmbeddingResult)
    actual_field_names = [f.name for f in dataclasses.fields(EmbeddingResult)]
    assert actual_field_names == list(expected)
    for name, expected_type in expected.items():
        assert hints[name] == expected_type, (
            f"EmbeddingResult.{name} type drifted: {hints[name]!r} != {expected_type!r}"
        )


def test_embedding_result_rejects_mutation() -> None:
    instance = EmbeddingResult(vector=(0.0,), model_id="m", model_version="v", dim=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.model_id = "other"  # type: ignore[misc]


# ---- StoredEmbedding ------------------------------------------------------


def test_stored_embedding_is_frozen_and_slotted() -> None:
    params = _dc_params(StoredEmbedding)
    assert params.frozen is True
    assert params.slots is True
    instance = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="m",
        model_version="v",
        vector=(0.0,),
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    assert not hasattr(instance, "__dict__")


def test_stored_embedding_fields_and_types() -> None:
    expected = {
        "entity_type": str,
        "entity_id": str,
        "model_id": str,
        "model_version": str,
        "vector": tuple[float, ...],
        "created_at": datetime,
    }
    hints = get_type_hints(StoredEmbedding)
    actual_field_names = [f.name for f in dataclasses.fields(StoredEmbedding)]
    assert actual_field_names == list(expected)
    for name, expected_type in expected.items():
        assert hints[name] == expected_type, (
            f"StoredEmbedding.{name} type drifted: {hints[name]!r} != {expected_type!r}"
        )


def test_stored_embedding_rejects_mutation() -> None:
    instance = StoredEmbedding(
        entity_type="task",
        entity_id="01J0",
        model_id="m",
        model_version="v",
        vector=(0.0,),
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.entity_id = "other"  # type: ignore[misc]


# ---- RecallHit ------------------------------------------------------------


def test_recall_hit_is_frozen_and_slotted() -> None:
    params = _dc_params(RecallHit)
    assert params.frozen is True
    assert params.slots is True
    instance = RecallHit(entity_type="task", entity_id="01J0", score=0.9, vector=(0.0,))
    assert not hasattr(instance, "__dict__")


def test_recall_hit_fields_and_types() -> None:
    expected = {
        "entity_type": str,
        "entity_id": str,
        "score": float,
        "vector": tuple[float, ...],
    }
    hints = get_type_hints(RecallHit)
    actual_field_names = [f.name for f in dataclasses.fields(RecallHit)]
    assert actual_field_names == list(expected)
    for name, expected_type in expected.items():
        assert hints[name] == expected_type, (
            f"RecallHit.{name} type drifted: {hints[name]!r} != {expected_type!r}"
        )


def test_recall_hit_rejects_mutation() -> None:
    instance = RecallHit(entity_type="task", entity_id="01J0", score=0.9, vector=(0.0,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.score = 0.1  # type: ignore[misc]
