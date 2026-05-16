"""Task aggregate events (ADR-0002).

Phase 1 ships three semantic task events:

- :class:`TaskCreated` — a task was registered with a title and optional body.
- :class:`TaskActivated` — the task transitioned to the active state.
- :class:`TaskCompleted` — the task was finished, optionally with a note.

``aggregate_id`` is the task's own ULID for all three events; that is how the
projector groups events into a single task row.

The :data:`TaskEvent` discriminated union lets callers deserialise an event
record without knowing its concrete type in advance::

    from pydantic import TypeAdapter

    TaskEventAdapter = TypeAdapter[TaskEvent](TaskEvent)
    event = TaskEventAdapter.validate_python(row_payload)
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from opshub.domain.events.base import DomainEvent


class TaskCreated(DomainEvent):
    """A task was registered. Carries the immutable title/body the user typed."""

    event_type: Literal["task.created"] = "task.created"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    title: str = Field(min_length=1, max_length=200)
    body: str | None = None


class TaskActivated(DomainEvent):
    """A task transitioned from any non-active state into ``active``.

    State-transition events carry no payload beyond the ``DomainEvent`` base
    fields: the projector applies the transition by inspecting ``event_type``.
    """

    event_type: Literal["task.activated"] = "task.activated"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1


class TaskCompleted(DomainEvent):
    """A task was marked done.

    ``result_note`` is a free-form short note (e.g. "shipped in PR #42") kept
    on the event so the audit trail does not require a separate join.
    """

    event_type: Literal["task.completed"] = "task.completed"  # pyright: ignore[reportIncompatibleVariableOverride]
    schema_version: int = 1
    result_note: str | None = None


TaskEvent = Annotated[
    TaskCreated | TaskActivated | TaskCompleted,
    Field(discriminator="event_type"),
]
"""Discriminated union of all task-aggregate events.

Use :class:`pydantic.TypeAdapter` to validate raw payloads against this union;
Pydantic dispatches to the right subclass via the ``event_type`` field.
"""
