"""Tests for the Phase 5 briefing domain events.

Covers all 3 new event classes plus the :data:`Phase5Event` and the
extended :data:`AllEvent` discriminated unions. The shape mirrors
``test_phase4.py`` so the conventions stay obvious to future readers:

- happy-path construction
- field validation (length bounds, non-negative token counts)
- ``frozen=True`` and ``extra="forbid"`` invariants
- ``source_refs`` round-trips as a list of ``(str, str)`` tuples
- ``occurred_at`` / ``recorded_at`` honour ``AfterValidator(to_utc)``
- round-trip through each union's ``TypeAdapter``
- the extended ``AllEvent`` still dispatches to Phase 1 / 2 / 3 / 4 events
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from opshub.core.errors import ValidationError as OpsHubValidationError
from opshub.core.ids import new_ulid
from opshub.domain.events import (
    AllEvent,
    BriefingFailed,
    BriefingGenerated,
    BriefingRequested,
    EmbeddingFailed,
    ItemEnqueued,
    Phase5Event,
    SourceObserved,
    TaskCreated,
    TextEmbedded,
)

# Module-level singletons so each test pays the schema-build cost once.
_Phase5Adapter: TypeAdapter[Phase5Event] = TypeAdapter(Phase5Event)  # pyright: ignore[reportCallIssue]
_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)  # pyright: ignore[reportCallIssue]


def _agg() -> str:
    return new_ulid()


# ---- BriefingRequested -----------------------------------------------------


def test_briefing_requested_minimal_fields() -> None:
    briefing_id = _agg()
    event = BriefingRequested(
        aggregate_id=briefing_id,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="Phase 5 status",
        scope="all",
        requested_by="cli:brief",
    )
    assert event.event_type == "briefing.requested"
    assert event.schema_version == 1
    assert event.topic == "Phase 5 status"
    assert event.scope == "all"
    assert event.requested_by == "cli:brief"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("briefing_id", "x" * 25),
        ("briefing_id", "x" * 27),
        ("briefing_id", ""),
        ("topic", ""),
        ("topic", "x" * 501),
        ("scope", ""),
        ("scope", "x" * 201),
        ("requested_by", ""),
        ("requested_by", "x" * 201),
    ],
)
def test_briefing_requested_rejects_out_of_range_strings(field: str, value: str) -> None:
    briefing_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": briefing_id,
        "actor": "cli:brief",
        "briefing_id": briefing_id,
        "topic": "topic",
        "scope": "all",
        "requested_by": "cli:brief",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        BriefingRequested(**payload)


def test_briefing_requested_rejects_wrong_event_type_literal() -> None:
    briefing_id = _agg()
    with pytest.raises(PydanticValidationError):
        BriefingRequested.model_validate(
            {
                "event_type": "briefing.invented",
                "aggregate_id": briefing_id,
                "actor": "cli:brief",
                "briefing_id": briefing_id,
                "topic": "topic",
                "scope": "all",
                "requested_by": "cli:brief",
            }
        )


# ---- BriefingGenerated -----------------------------------------------------


def test_briefing_generated_minimal_fields() -> None:
    briefing_id = _agg()
    event = BriefingGenerated(
        aggregate_id=briefing_id,
        actor="service:briefing",
        briefing_id=briefing_id,
        topic="Phase 5 status",
        scope="all",
        markdown="# Briefing\n\nbody",
        source_refs=[("task", _agg()), ("decision", _agg())],
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=1234,
        tokens_out=567,
    )
    assert event.event_type == "briefing.generated"
    assert event.schema_version == 1
    assert event.tokens_in == 1234
    assert event.tokens_out == 567
    assert len(event.source_refs) == 2
    assert event.source_refs[0][0] == "task"


def test_briefing_generated_accepts_empty_source_refs() -> None:
    """``source_refs`` defaults to an empty list (e.g. zero-recall topic)."""
    briefing_id = _agg()
    event = BriefingGenerated(
        aggregate_id=briefing_id,
        actor="service:briefing",
        briefing_id=briefing_id,
        topic="empty-topic",
        scope="all",
        markdown="# Empty briefing",
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=0,
        tokens_out=0,
    )
    assert event.source_refs == []


def test_briefing_generated_rejects_negative_tokens() -> None:
    briefing_id = _agg()
    with pytest.raises(PydanticValidationError):
        BriefingGenerated(
            aggregate_id=briefing_id,
            actor="service:briefing",
            briefing_id=briefing_id,
            topic="t",
            scope="all",
            markdown="x",
            source_refs=[],
            model_id="claude-haiku-4-5-20251001",
            model_version="20251001",
            tokens_in=-1,
            tokens_out=0,
        )
    with pytest.raises(PydanticValidationError):
        BriefingGenerated(
            aggregate_id=briefing_id,
            actor="service:briefing",
            briefing_id=briefing_id,
            topic="t",
            scope="all",
            markdown="x",
            source_refs=[],
            model_id="claude-haiku-4-5-20251001",
            model_version="20251001",
            tokens_in=0,
            tokens_out=-1,
        )


def test_briefing_generated_rejects_empty_markdown() -> None:
    briefing_id = _agg()
    with pytest.raises(PydanticValidationError):
        BriefingGenerated(
            aggregate_id=briefing_id,
            actor="service:briefing",
            briefing_id=briefing_id,
            topic="t",
            scope="all",
            markdown="",
            source_refs=[],
            model_id="claude-haiku-4-5-20251001",
            model_version="20251001",
            tokens_in=0,
            tokens_out=0,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("briefing_id", "x" * 25),
        ("briefing_id", ""),
        ("topic", ""),
        ("topic", "x" * 501),
        ("scope", ""),
        ("scope", "x" * 201),
        ("model_id", ""),
        ("model_id", "x" * 201),
        ("model_version", ""),
        ("model_version", "x" * 101),
    ],
)
def test_briefing_generated_rejects_out_of_range_strings(field: str, value: str) -> None:
    briefing_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": briefing_id,
        "actor": "service:briefing",
        "briefing_id": briefing_id,
        "topic": "t",
        "scope": "all",
        "markdown": "x",
        "source_refs": [],
        "model_id": "claude-haiku-4-5-20251001",
        "model_version": "20251001",
        "tokens_in": 0,
        "tokens_out": 0,
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        BriefingGenerated(**payload)


def test_briefing_generated_source_refs_roundtrip_as_tuples() -> None:
    """``source_refs`` survives ``model_dump`` → ``validate_python`` round-trip."""
    briefing_id = _agg()
    task_id = _agg()
    decision_id = _agg()
    inbox_id = _agg()
    refs = [
        ("task", task_id),
        ("decision", decision_id),
        ("inbox_item", inbox_id),
    ]
    event = BriefingGenerated(
        aggregate_id=briefing_id,
        actor="service:briefing",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        markdown="# Briefing",
        source_refs=refs,
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=10,
        tokens_out=20,
    )
    dumped = event.model_dump(mode="json")
    # JSON has no native tuple type; pydantic emits a list of lists.
    assert dumped["source_refs"] == [
        ["task", task_id],
        ["decision", decision_id],
        ["inbox_item", inbox_id],
    ]
    restored = BriefingGenerated.model_validate(dumped)
    # On re-validation pydantic coerces the inner lists back into tuples
    # because the field annotation is ``list[tuple[str, str]]``.
    assert restored.source_refs == [
        ("task", task_id),
        ("decision", decision_id),
        ("inbox_item", inbox_id),
    ]
    assert all(isinstance(ref, tuple) for ref in restored.source_refs)


# ---- BriefingFailed --------------------------------------------------------


def test_briefing_failed_minimal_fields() -> None:
    briefing_id = _agg()
    event = BriefingFailed(
        aggregate_id=briefing_id,
        actor="service:briefing",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        model_id="claude-haiku-4-5-20251001",
        error_message="upstream HTTP 500",
    )
    assert event.event_type == "briefing.failed"
    assert event.schema_version == 1
    assert event.error_message == "upstream HTTP 500"


def test_briefing_failed_accepts_max_length_error_message() -> None:
    briefing_id = _agg()
    event = BriefingFailed(
        aggregate_id=briefing_id,
        actor="service:briefing",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        model_id="claude-haiku-4-5-20251001",
        error_message="x" * 2000,
    )
    assert len(event.error_message) == 2000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("briefing_id", "x" * 25),
        ("briefing_id", ""),
        ("topic", ""),
        ("topic", "x" * 501),
        ("scope", ""),
        ("scope", "x" * 201),
        ("model_id", ""),
        ("model_id", "x" * 201),
        ("error_message", ""),
        ("error_message", "x" * 2001),
    ],
)
def test_briefing_failed_rejects_out_of_range_strings(field: str, value: str) -> None:
    briefing_id = _agg()
    payload: dict[str, Any] = {
        "aggregate_id": briefing_id,
        "actor": "service:briefing",
        "briefing_id": briefing_id,
        "topic": "t",
        "scope": "all",
        "model_id": "claude-haiku-4-5-20251001",
        "error_message": "boom",
    }
    payload[field] = value
    with pytest.raises(PydanticValidationError):
        BriefingFailed(**payload)


# ---- frozen / extra=forbid / Literal-locked event_type --------------------


def test_phase5_event_is_frozen() -> None:
    briefing_id = _agg()
    event = BriefingRequested(
        aggregate_id=briefing_id,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        requested_by="cli:brief",
    )
    with pytest.raises(PydanticValidationError):
        event.topic = "other"


def test_phase5_event_forbids_extra_fields() -> None:
    briefing_id = _agg()
    with pytest.raises(PydanticValidationError):
        BriefingRequested.model_validate(
            {
                "aggregate_id": briefing_id,
                "actor": "cli:brief",
                "briefing_id": briefing_id,
                "topic": "t",
                "scope": "all",
                "requested_by": "cli:brief",
                "unexpected": "boom",
            }
        )


# ---- tz-aware datetime invariants -----------------------------------------


def test_briefing_event_default_datetimes_are_tz_aware_utc() -> None:
    """``occurred_at`` / ``recorded_at`` default to tz-aware UTC."""
    briefing_id = _agg()
    event = BriefingRequested(
        aggregate_id=briefing_id,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        requested_by="cli:brief",
    )
    assert event.occurred_at.tzinfo is not None
    assert event.recorded_at.tzinfo is not None
    assert event.occurred_at.utcoffset() == timedelta(0)
    assert event.recorded_at.utcoffset() == timedelta(0)


def test_briefing_event_rejects_naive_datetime() -> None:
    """``AfterValidator(to_utc)`` raises on naive datetime input."""
    briefing_id = _agg()
    naive = datetime(2026, 5, 17, 12, 0, 0)  # intentional naive
    # `to_utc` raises ``opshub.core.errors.ValidationError``; pydantic
    # wraps non-pydantic exceptions raised by validators in a
    # PydanticValidationError, but the underlying type is still
    # surfaced through the chain. We accept either form here so the
    # test stays robust across pydantic patch releases.
    with pytest.raises((PydanticValidationError, OpsHubValidationError)):
        BriefingRequested(
            aggregate_id=briefing_id,
            actor="cli:brief",
            briefing_id=briefing_id,
            topic="t",
            scope="all",
            requested_by="cli:brief",
            occurred_at=naive,
        )


def test_briefing_event_normalises_non_utc_tz() -> None:
    """Non-UTC tz-aware values are converted to UTC, not rejected."""
    briefing_id = _agg()
    plus_nine = timezone(timedelta(hours=9))
    local = datetime(2026, 5, 17, 9, 0, 0, tzinfo=plus_nine)
    event = BriefingRequested(
        aggregate_id=briefing_id,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        requested_by="cli:brief",
        occurred_at=local,
    )
    assert event.occurred_at == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)
    assert event.occurred_at.utcoffset() == timedelta(0)


# ---- Phase5Event discriminated union --------------------------------------


def _factory_briefing_requested() -> BriefingRequested:
    briefing_id = _agg()
    return BriefingRequested(
        aggregate_id=briefing_id,
        actor="cli:brief",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        requested_by="cli:brief",
    )


def _factory_briefing_generated() -> BriefingGenerated:
    briefing_id = _agg()
    return BriefingGenerated(
        aggregate_id=briefing_id,
        actor="service:briefing",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        markdown="# body",
        source_refs=[("task", _agg())],
        model_id="claude-haiku-4-5-20251001",
        model_version="20251001",
        tokens_in=10,
        tokens_out=20,
    )


def _factory_briefing_failed() -> BriefingFailed:
    briefing_id = _agg()
    return BriefingFailed(
        aggregate_id=briefing_id,
        actor="service:briefing",
        briefing_id=briefing_id,
        topic="t",
        scope="all",
        model_id="claude-haiku-4-5-20251001",
        error_message="boom",
    )


_PHASE5_FACTORIES: list[tuple[str, Any]] = [
    ("briefing.requested", _factory_briefing_requested),
    ("briefing.generated", _factory_briefing_generated),
    ("briefing.failed", _factory_briefing_failed),
]


@pytest.mark.parametrize(
    ("event_type", "factory"),
    _PHASE5_FACTORIES,
    ids=[event_type for event_type, _ in _PHASE5_FACTORIES],
)
def test_phase5_event_roundtrip_via_model_dump(event_type: str, factory: Any) -> None:
    event = factory()
    assert event.event_type == event_type
    restored = _Phase5Adapter.validate_python(event.model_dump(mode="json"))
    assert restored == event
    assert type(restored) is type(event)


def test_phase5_event_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "briefing.invented",
        "aggregate_id": _agg(),
        "actor": "service:briefing",
    }
    with pytest.raises(PydanticValidationError):
        _Phase5Adapter.validate_python(payload)


def test_phase5_event_rejects_task_event_payload() -> None:
    """A ``task.created`` payload must NOT be accepted by Phase5Event."""
    payload = {
        "event_type": "task.created",
        "aggregate_id": _agg(),
        "actor": "cli:create",
        "title": "t",
    }
    with pytest.raises(PydanticValidationError):
        _Phase5Adapter.validate_python(payload)


def test_phase5_event_rejects_phase4_payload() -> None:
    """Phase 4 ``embedding.text_embedded`` must NOT be accepted by Phase5Event."""
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
    with pytest.raises(PydanticValidationError):
        _Phase5Adapter.validate_python(payload)


# ---- AllEvent extension ---------------------------------------------------


def test_all_event_dispatches_to_briefing_requested() -> None:
    """``AllEvent`` must decode the new Phase 5 request event."""
    briefing_id = _agg()
    payload = {
        "event_type": "briefing.requested",
        "aggregate_id": briefing_id,
        "actor": "cli:brief",
        "briefing_id": briefing_id,
        "topic": "t",
        "scope": "all",
        "requested_by": "cli:brief",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, BriefingRequested)


def test_all_event_dispatches_to_briefing_generated() -> None:
    briefing_id = _agg()
    payload = {
        "event_type": "briefing.generated",
        "aggregate_id": briefing_id,
        "actor": "service:briefing",
        "briefing_id": briefing_id,
        "topic": "t",
        "scope": "all",
        "markdown": "# body",
        "source_refs": [["task", _agg()]],
        "model_id": "claude-haiku-4-5-20251001",
        "model_version": "20251001",
        "tokens_in": 10,
        "tokens_out": 20,
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, BriefingGenerated)


def test_all_event_dispatches_to_briefing_failed() -> None:
    briefing_id = _agg()
    payload = {
        "event_type": "briefing.failed",
        "aggregate_id": briefing_id,
        "actor": "service:briefing",
        "briefing_id": briefing_id,
        "topic": "t",
        "scope": "all",
        "model_id": "claude-haiku-4-5-20251001",
        "error_message": "boom",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, BriefingFailed)


# ---- Backwards-compat: AllEvent still decodes prior phases ----------------


def test_all_event_still_dispatches_to_task_event() -> None:
    payload = {
        "event_type": "task.created",
        "aggregate_id": _agg(),
        "actor": "cli:create",
        "title": "still works",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, TaskCreated)


def test_all_event_still_dispatches_to_phase2_event() -> None:
    payload = {
        "event_type": "inbox.enqueued",
        "aggregate_id": _agg(),
        "actor": "cli:inbox",
        "summary": "from all-event",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ItemEnqueued)


def test_all_event_still_dispatches_to_phase3_event() -> None:
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


def test_all_event_still_dispatches_to_phase4_text_embedded() -> None:
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


def test_all_event_still_dispatches_to_phase4_embedding_failed() -> None:
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
