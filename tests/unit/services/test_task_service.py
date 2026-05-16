"""Tests for :class:`opshub.services.task_service.TaskService`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid
from opshub.domain.events import DomainEvent, TaskActivated, TaskCompleted, TaskCreated
from opshub.services.event_store import InMemoryEventStore
from opshub.services.projector import NoOpProjector
from opshub.services.task_service import TaskService


class _RecordingProjector:
    """Projector test double that captures applied events in order."""

    def __init__(self) -> None:
        self.applied: list[DomainEvent] = []

    def apply(self, event: DomainEvent) -> None:
        self.applied.append(event)


# ---- create_task ----------------------------------------------------------


def test_create_task_appends_task_created_event() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = TaskService(store=store, projector=projector)

    event = service.create_task(title="write ADR", body="draft of ADR-0017")

    assert isinstance(event, TaskCreated)
    assert event.title == "write ADR"
    assert event.body == "draft of ADR-0017"
    # The event in the store is the same instance returned to the caller.
    assert store.events == [event]
    # The projector saw the exact same event.
    assert projector.applied == [event]
    assert projector.applied[0] is event


def test_create_task_rejects_empty_title() -> None:
    service = TaskService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(PydanticValidationError):
        service.create_task(title="")


# ---- activate_task --------------------------------------------------------


def test_activate_task_appends_task_activated_event() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = TaskService(store=store, projector=projector)
    task_id = new_ulid()

    event = service.activate_task(task_id)

    assert isinstance(event, TaskActivated)
    assert event.aggregate_id == task_id
    assert store.events == [event]
    assert projector.applied == [event]


def test_activate_task_rejects_non_ulid() -> None:
    service = TaskService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.activate_task("not-a-ulid")


def test_activate_task_rejects_wrong_length_ulid() -> None:
    """A 26-char string that decodes to >128 bits must be rejected too."""
    service = TaskService(store=InMemoryEventStore(), projector=NoOpProjector())
    # Leading 'Z' makes the value exceed 128 bits (top char must be 0-7).
    with pytest.raises(ValidationError):
        service.activate_task("Z" * 26)


# ---- complete_task --------------------------------------------------------


def test_complete_task_appends_task_completed_event_with_result_note() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = TaskService(store=store, projector=projector)
    task_id = new_ulid()

    event = service.complete_task(task_id, "shipped")

    assert isinstance(event, TaskCompleted)
    assert event.aggregate_id == task_id
    assert event.result_note == "shipped"
    assert store.events == [event]
    assert projector.applied == [event]


def test_complete_task_rejects_non_ulid() -> None:
    service = TaskService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.complete_task("garbage", "note")


# ---- actor stamping -------------------------------------------------------


def test_actor_defaults_to_cli_default_and_is_stamped_on_each_event() -> None:
    store = InMemoryEventStore()
    service = TaskService(store=store, projector=NoOpProjector())

    created = service.create_task(title="t1")
    activated = service.activate_task(created.aggregate_id)
    completed = service.complete_task(created.aggregate_id, "done")

    assert created.actor == "cli:default"
    assert activated.actor == "cli:default"
    assert completed.actor == "cli:default"


def test_custom_actor_is_stamped_on_each_event() -> None:
    store = InMemoryEventStore()
    service = TaskService(store=store, projector=NoOpProjector(), actor="agent:planner")

    created = service.create_task(title="t1")
    activated = service.activate_task(created.aggregate_id)
    completed = service.complete_task(created.aggregate_id)

    for event in (created, activated, completed):
        assert event.actor == "agent:planner"


# ---- projector ordering ---------------------------------------------------


def test_projector_receives_events_in_command_order() -> None:
    """Across three commands, the projector must see events in append order.

    This is not a count-only assertion: we assert the exact sequence of
    ``event_type`` discriminators *and* that each applied event is the same
    instance the store recorded.
    """
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = TaskService(store=store, projector=projector)

    created = service.create_task(title="ordering test")
    activated = service.activate_task(created.aggregate_id)
    completed = service.complete_task(created.aggregate_id, "ok")

    expected_sequence = [created, activated, completed]
    assert projector.applied == expected_sequence
    # Identity, not just equality: the projector must see the same instances
    # the store received, in the same order.
    for applied, expected in zip(projector.applied, expected_sequence, strict=True):
        assert applied is expected
    # The store and the projector agree on order.
    assert store.events == projector.applied
    # And the discriminator sequence is exactly create -> activate -> complete.
    assert [e.event_type for e in projector.applied] == [
        "task.created",
        "task.activated",
        "task.completed",
    ]
