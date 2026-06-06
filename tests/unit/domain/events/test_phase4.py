"""Tests for the Phase 4 domain events.

Covers all 3 new event classes plus their dispatch through the unified
:data:`AllEvent` discriminated union. The shape mirrors ``test_phase3.py``
so the conventions stay obvious to future readers:

- happy-path construction
- field validation (length bounds, ``Literal`` enums)
- ``frozen=True`` and ``extra="forbid"`` invariants
- round-trip through ``AllEvent``'s ``TypeAdapter``
- ``AllEvent`` still dispatches to Phase 1 / 2 / 3 events

Phase-scoped grouping aliases (``Phase2Event`` ... ``Phase8Event``) were
dropped in epic #470 — :data:`AllEvent` is the single discriminated
union over every event family OpsHub knows how to decode.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from opshub.core.ids import new_ulid
from opshub.domain.events import (
    AllEvent,
    EmbeddingFailed,
    EmbeddingRebuildRequested,
    ItemEnqueued,
    SourceObserved,
    TaskCreated,
    TextEmbedded,
)

# Module-level singleton so each test pays the schema-build cost once.
_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)  # pyright: ignore[reportCallIssue]


def _agg() -> str:
    return new_ulid()


# ---- TextEmbedded ----------------------------------------------------------


def test_text_embedded_minimal_fields() -> None:
    entity_id = _agg()
    event = TextEmbedded(
        aggregate_id=entity_id,
        actor="service:embedding",
        entity_type="task",
        entity_id=entity_id,
        model_id="BAAI/bge-m3",
        model_version="1.0.0",
        dim=1024,
    )
    assert event.event_type == "embedding.text_embedded"
    assert event.schema_version == 1
    assert event.entity_type == "task"
    assert event.dim == 1024


@pytest.mark.parametrize("entity_type", ["task", "decision", "inbox_item", "source"])
def test_text_embedded_accepts_allowed_entity_types(entity_type: str) -> None:
    entity_id = _agg()
    event = TextEmbedded(
        aggregate_id=entity_id,
        actor="service:embedding",
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_id=entity_id,
        model_id="BAAI/bge-m3",
        model_version="1.0.0",
        dim=1024,
    )
    assert event.entity_type == entity_type


def test_text_embedded_rejects_unknown_entity_type() -> None:
    entity_id = _agg()
    with pytest.raises(PydanticValidationError):
        TextEmbedded(
            aggregate_id=entity_id,
            actor="service:embedding",
            entity_type="project",  # type: ignore[arg-type]
            entity_id=entity_id,
            model_id="BAAI/bge-m3",
            model_version="1.0.0",
            dim=1024,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_id", "x" * 25),
        ("entity_id", "x" * 27),
        ("entity_id", ""),
        ("model_id", ""),
        ("model_id", "x" * 201),
        ("model_version", ""),
        ("model_version", "x" * 101),
    ],
)
def test_text_embedded_rejects_out_of_range_strings(field: str, value: str) -> None:
    entity_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": entity_id,
        "actor": "service:embedding",
        "entity_type": "task",
        "entity_id": entity_id,
        "model_id": "BAAI/bge-m3",
        "model_version": "1.0.0",
        "dim": 1024,
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        TextEmbedded(**payload)


def test_text_embedded_rejects_non_positive_dim() -> None:
    entity_id = _agg()
    with pytest.raises(PydanticValidationError):
        TextEmbedded(
            aggregate_id=entity_id,
            actor="service:embedding",
            entity_type="task",
            entity_id=entity_id,
            model_id="BAAI/bge-m3",
            model_version="1.0.0",
            dim=0,
        )


def test_text_embedded_requires_dim() -> None:
    entity_id = _agg()
    with pytest.raises(PydanticValidationError):
        TextEmbedded.model_validate(
            {
                "aggregate_id": entity_id,
                "actor": "service:embedding",
                "entity_type": "task",
                "entity_id": entity_id,
                "model_id": "BAAI/bge-m3",
                "model_version": "1.0.0",
            }
        )


def test_text_embedded_accepts_max_length_strings() -> None:
    entity_id = _agg()
    event = TextEmbedded(
        aggregate_id=entity_id,
        actor="service:embedding",
        entity_type="source",
        entity_id=entity_id,
        model_id="x" * 200,
        model_version="x" * 100,
        dim=1536,
    )
    assert len(event.model_id) == 200
    assert len(event.model_version) == 100


# ---- EmbeddingRebuildRequested --------------------------------------------


def test_embedding_rebuild_requested_minimal_fields() -> None:
    event = EmbeddingRebuildRequested(
        aggregate_id=_agg(),
        actor="cli:embeddings_rebuild",
        scope="all",
        model_id="BAAI/bge-m3",
        model_version="1.0.0",
    )
    assert event.event_type == "embedding.rebuild_requested"
    assert event.schema_version == 1
    assert event.scope == "all"


@pytest.mark.parametrize(
    "scope",
    [
        "all",
        "entity_type:task",
        "entity_type:decision",
        "entity_type:inbox_item",
        "entity_type:source",
    ],
)
def test_embedding_rebuild_requested_accepts_documented_scopes(scope: str) -> None:
    event = EmbeddingRebuildRequested(
        aggregate_id=_agg(),
        actor="cli:embeddings_rebuild",
        scope=scope,
        model_id="BAAI/bge-m3",
        model_version="1.0.0",
    )
    assert event.scope == scope


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", ""),
        ("scope", "x" * 201),
        ("model_id", ""),
        ("model_id", "x" * 201),
        ("model_version", ""),
        ("model_version", "x" * 101),
    ],
)
def test_embedding_rebuild_requested_rejects_out_of_range_strings(field: str, value: str) -> None:
    payload: dict[str, Any] = {
        "aggregate_id": _agg(),
        "actor": "cli:embeddings_rebuild",
        "scope": "all",
        "model_id": "BAAI/bge-m3",
        "model_version": "1.0.0",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        EmbeddingRebuildRequested(**payload)


def test_embedding_rebuild_requested_requires_model_id() -> None:
    with pytest.raises(PydanticValidationError):
        EmbeddingRebuildRequested.model_validate(
            {
                "aggregate_id": _agg(),
                "actor": "cli:embeddings_rebuild",
                "scope": "all",
                "model_version": "1.0.0",
            }
        )


# ---- EmbeddingFailed -------------------------------------------------------


def test_embedding_failed_minimal_fields() -> None:
    entity_id = _agg()
    event = EmbeddingFailed(
        aggregate_id=entity_id,
        actor="service:embedding",
        entity_type="task",
        entity_id=entity_id,
        model_id="BAAI/bge-m3",
        error_message="upstream HTTP 500",
    )
    assert event.event_type == "embedding.failed"
    assert event.schema_version == 1
    assert event.error_message == "upstream HTTP 500"


@pytest.mark.parametrize("entity_type", ["task", "decision", "inbox_item", "source"])
def test_embedding_failed_accepts_allowed_entity_types(entity_type: str) -> None:
    entity_id = _agg()
    event = EmbeddingFailed(
        aggregate_id=entity_id,
        actor="service:embedding",
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_id=entity_id,
        model_id="BAAI/bge-m3",
        error_message="boom",
    )
    assert event.entity_type == entity_type


def test_embedding_failed_rejects_unknown_entity_type() -> None:
    entity_id = _agg()
    with pytest.raises(PydanticValidationError):
        EmbeddingFailed(
            aggregate_id=entity_id,
            actor="service:embedding",
            entity_type="project",  # type: ignore[arg-type]
            entity_id=entity_id,
            model_id="BAAI/bge-m3",
            error_message="boom",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_id", "x" * 25),
        ("entity_id", "x" * 27),
        ("entity_id", ""),
        ("model_id", ""),
        ("model_id", "x" * 201),
        ("error_message", ""),
        ("error_message", "x" * 2001),
    ],
)
def test_embedding_failed_rejects_out_of_range_strings(field: str, value: str) -> None:
    entity_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": entity_id,
        "actor": "service:embedding",
        "entity_type": "task",
        "entity_id": entity_id,
        "model_id": "BAAI/bge-m3",
        "error_message": "boom",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        EmbeddingFailed(**payload)


def test_embedding_failed_accepts_max_length_error_message() -> None:
    entity_id = _agg()
    event = EmbeddingFailed(
        aggregate_id=entity_id,
        actor="service:embedding",
        entity_type="task",
        entity_id=entity_id,
        model_id="BAAI/bge-m3",
        error_message="x" * 2000,
    )
    assert len(event.error_message) == 2000


# ---- frozen / extra=forbid / Literal-locked event_type --------------------


def test_phase4_event_is_frozen() -> None:
    entity_id = _agg()
    event = TextEmbedded(
        aggregate_id=entity_id,
        actor="service:embedding",
        entity_type="task",
        entity_id=entity_id,
        model_id="BAAI/bge-m3",
        model_version="1.0.0",
        dim=1024,
    )
    with pytest.raises(PydanticValidationError):
        event.model_id = "other"


def test_phase4_event_forbids_extra_fields() -> None:
    entity_id = _agg()
    with pytest.raises(PydanticValidationError):
        TextEmbedded.model_validate(
            {
                "aggregate_id": entity_id,
                "actor": "service:embedding",
                "entity_type": "task",
                "entity_id": entity_id,
                "model_id": "BAAI/bge-m3",
                "model_version": "1.0.0",
                "dim": 1024,
                "unexpected": "boom",
            }
        )


def test_text_embedded_rejects_wrong_event_type_literal() -> None:
    """The ``event_type`` Literal cannot be overridden to an arbitrary value."""
    entity_id = _agg()
    with pytest.raises(PydanticValidationError):
        TextEmbedded.model_validate(
            {
                "event_type": "embedding.invented",
                "aggregate_id": entity_id,
                "actor": "service:embedding",
                "entity_type": "task",
                "entity_id": entity_id,
                "model_id": "BAAI/bge-m3",
                "model_version": "1.0.0",
                "dim": 1024,
            }
        )


def test_embedding_rebuild_requested_rejects_wrong_event_type_literal() -> None:
    with pytest.raises(PydanticValidationError):
        EmbeddingRebuildRequested.model_validate(
            {
                "event_type": "embedding.invented",
                "aggregate_id": _agg(),
                "actor": "cli:embeddings_rebuild",
                "scope": "all",
                "model_id": "BAAI/bge-m3",
                "model_version": "1.0.0",
            }
        )


def test_embedding_failed_rejects_wrong_event_type_literal() -> None:
    entity_id = _agg()
    with pytest.raises(PydanticValidationError):
        EmbeddingFailed.model_validate(
            {
                "event_type": "embedding.invented",
                "aggregate_id": entity_id,
                "actor": "service:embedding",
                "entity_type": "task",
                "entity_id": entity_id,
                "model_id": "BAAI/bge-m3",
                "error_message": "boom",
            }
        )


# ---- AllEvent dispatch for Phase 4 event types ----------------------------


_PHASE4_FACTORIES: list[tuple[str, Any]] = [
    (
        "embedding.text_embedded",
        lambda: TextEmbedded(
            aggregate_id=_agg(),
            actor="service:embedding",
            entity_type="task",
            entity_id=_agg(),
            model_id="BAAI/bge-m3",
            model_version="1.0.0",
            dim=1024,
        ),
    ),
    (
        "embedding.rebuild_requested",
        lambda: EmbeddingRebuildRequested(
            aggregate_id=_agg(),
            actor="cli:embeddings_rebuild",
            scope="all",
            model_id="BAAI/bge-m3",
            model_version="1.0.0",
        ),
    ),
    (
        "embedding.failed",
        lambda: EmbeddingFailed(
            aggregate_id=_agg(),
            actor="service:embedding",
            entity_type="source",
            entity_id=_agg(),
            model_id="BAAI/bge-m3",
            error_message="boom",
        ),
    ),
]


@pytest.mark.parametrize(
    ("event_type", "factory"),
    _PHASE4_FACTORIES,
    ids=[event_type for event_type, _ in _PHASE4_FACTORIES],
)
def test_phase4_event_roundtrip_via_model_dump(event_type: str, factory: Any) -> None:
    event = factory()
    assert event.event_type == event_type
    restored = _AllEventAdapter.validate_python(event.model_dump(mode="json"))
    assert restored == event
    assert type(restored) is type(event)


def test_phase4_event_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "embedding.invented",
        "aggregate_id": _agg(),
        "actor": "service:embedding",
    }
    with pytest.raises(PydanticValidationError):
        _AllEventAdapter.validate_python(payload)


# ---- AllEvent extension ---------------------------------------------------


def test_all_event_dispatches_to_task_event() -> None:
    """Backwards-compat: ``AllEvent`` must still decode Phase 1 task events."""
    payload = {
        "event_type": "task.created",
        "aggregate_id": _agg(),
        "actor": "cli:create",
        "title": "still works",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TaskCreated)


def test_all_event_dispatches_to_phase2_event() -> None:
    """Backwards-compat: ``AllEvent`` must still decode Phase 2 events."""
    payload = {
        "event_type": "inbox.enqueued",
        "aggregate_id": _agg(),
        "actor": "cli:inbox",
        "summary": "from all-event",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ItemEnqueued)


def test_all_event_dispatches_to_phase3_event() -> None:
    """Backwards-compat: ``AllEvent`` must still decode Phase 3 events."""
    payload = {
        "event_type": "source.observed",
        "aggregate_id": _agg(),
        "actor": "connector:github",
        "connector_name": "github",
        "external_id": "owner/repo#1",
        "source_type": "issue",
        "title": "from all-event",
        # epic #470 / issue #481: ``body`` is required + non-empty.
        "body": "from all-event body",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, SourceObserved)


def test_all_event_dispatches_to_text_embedded() -> None:
    """Forwards-compat: ``AllEvent`` must decode the Phase 4 success event."""
    entity_id = _agg()
    payload = {
        "event_type": "embedding.text_embedded",
        "aggregate_id": entity_id,
        "actor": "service:embedding",
        "entity_type": "task",
        "entity_id": entity_id,
        "model_id": "BAAI/bge-m3",
        "model_version": "1.0.0",
        "dim": 1024,
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TextEmbedded)


def test_all_event_dispatches_to_embedding_rebuild_requested() -> None:
    """``AllEvent`` must decode the ``embedding.rebuild_requested`` family too."""
    payload = {
        "event_type": "embedding.rebuild_requested",
        "aggregate_id": _agg(),
        "actor": "cli:embeddings_rebuild",
        "scope": "all",
        "model_id": "BAAI/bge-m3",
        "model_version": "1.0.0",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, EmbeddingRebuildRequested)


def test_all_event_dispatches_to_embedding_failed() -> None:
    """``AllEvent`` must decode the ``embedding.failed`` family too."""
    entity_id = _agg()
    payload = {
        "event_type": "embedding.failed",
        "aggregate_id": entity_id,
        "actor": "service:embedding",
        "entity_type": "task",
        "entity_id": entity_id,
        "model_id": "BAAI/bge-m3",
        "error_message": "boom",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, EmbeddingFailed)


def test_all_event_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "phase42.future",
        "aggregate_id": _agg(),
        "actor": "cli:future",
    }
    with pytest.raises(PydanticValidationError):
        _AllEventAdapter.validate_python(payload)
