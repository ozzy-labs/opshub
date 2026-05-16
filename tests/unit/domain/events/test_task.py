"""Tests for opshub.domain.events.task.

Cover the three concrete task events plus the discriminated-union TypeAdapter
that downstream service / projector code will use to deserialise rows from the
event store.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from opshub.core.ids import new_ulid
from opshub.domain.events import (
    TaskActivated,
    TaskCompleted,
    TaskCreated,
    TaskEvent,
)

# TypeAdapter expects a runtime value; the generic parameter pins the static
# type of values returned by ``validate_python`` so pyright sees the union
# instead of ``Unknown``.
_TaskEventAdapter: TypeAdapter[TaskEvent] = TypeAdapter(TaskEvent)  # pyright: ignore[reportCallIssue]


def _aggregate_id() -> str:
    return new_ulid()


def test_task_created_minimal_fields() -> None:
    event = TaskCreated(aggregate_id=_aggregate_id(), actor="cli:create", title="write tests")
    assert event.event_type == "task.created"
    assert event.schema_version == 1
    assert event.title == "write tests"
    assert event.body is None


def test_task_created_rejects_empty_title() -> None:
    with pytest.raises(PydanticValidationError):
        TaskCreated(aggregate_id=_aggregate_id(), actor="cli:create", title="")


def test_task_created_rejects_overlong_title() -> None:
    with pytest.raises(PydanticValidationError):
        TaskCreated(aggregate_id=_aggregate_id(), actor="cli:create", title="x" * 201)


def test_task_created_accepts_max_title_length() -> None:
    event = TaskCreated(aggregate_id=_aggregate_id(), actor="cli:create", title="x" * 200)
    assert len(event.title) == 200


def test_task_activated_has_no_extra_fields() -> None:
    event = TaskActivated(aggregate_id=_aggregate_id(), actor="agent:claude")
    assert event.event_type == "task.activated"
    assert event.schema_version == 1


def test_task_completed_with_and_without_note() -> None:
    agg = _aggregate_id()
    plain = TaskCompleted(aggregate_id=agg, actor="cli:complete")
    annotated = TaskCompleted(aggregate_id=agg, actor="cli:complete", result_note="shipped")
    assert plain.event_type == "task.completed"
    assert plain.result_note is None
    assert annotated.result_note == "shipped"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TaskCreated(aggregate_id=_aggregate_id(), actor="cli:create", title="t"),
        lambda: TaskActivated(aggregate_id=_aggregate_id(), actor="cli:activate"),
        lambda: TaskCompleted(aggregate_id=_aggregate_id(), actor="cli:complete", result_note="ok"),
    ],
    ids=["created", "activated", "completed"],
)
def test_roundtrip_via_model_dump(factory: Any) -> None:
    event: TaskEvent = factory()
    dumped = event.model_dump(mode="json")
    restored = _TaskEventAdapter.validate_python(dumped)
    assert restored == event
    assert type(restored) is type(event)


def test_discriminated_union_dispatches_to_task_created() -> None:
    agg = _aggregate_id()
    payload = {
        "event_type": "task.created",
        "aggregate_id": agg,
        "actor": "cli:create",
        "title": "discriminate me",
    }
    event = _TaskEventAdapter.validate_python(payload)
    assert isinstance(event, TaskCreated)
    assert event.title == "discriminate me"


def test_discriminated_union_dispatches_to_task_activated() -> None:
    payload = {
        "event_type": "task.activated",
        "aggregate_id": _aggregate_id(),
        "actor": "agent:claude",
    }
    event = _TaskEventAdapter.validate_python(payload)
    assert isinstance(event, TaskActivated)


def test_discriminated_union_dispatches_to_task_completed() -> None:
    payload = {
        "event_type": "task.completed",
        "aggregate_id": _aggregate_id(),
        "actor": "cli:complete",
        "result_note": "done",
    }
    event = _TaskEventAdapter.validate_python(payload)
    assert isinstance(event, TaskCompleted)
    assert event.result_note == "done"


def test_discriminated_union_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "task.frobnicated",
        "aggregate_id": _aggregate_id(),
        "actor": "cli:create",
    }
    with pytest.raises(PydanticValidationError):
        _TaskEventAdapter.validate_python(payload)


def test_extra_fields_forbidden_on_task_created() -> None:
    with pytest.raises(PydanticValidationError):
        TaskCreated.model_validate(
            {
                "aggregate_id": _aggregate_id(),
                "actor": "cli:create",
                "title": "t",
                "unexpected": "boom",
            }
        )


def test_task_event_is_frozen() -> None:
    event = TaskCreated(aggregate_id=_aggregate_id(), actor="cli:create", title="t")
    with pytest.raises(PydanticValidationError):
        event.title = "mutated"


def test_schema_version_can_be_overridden_for_migrations() -> None:
    # Persisted historical events may carry an older schema_version. The model
    # must accept that without rejecting it — only the default is locked to 1.
    event = TaskCreated(
        aggregate_id=_aggregate_id(),
        actor="cli:create",
        title="old",
        schema_version=2,
    )
    assert event.schema_version == 2
