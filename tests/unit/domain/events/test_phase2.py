"""Tests for the Phase 2 domain events.

Covers all 11 new event classes plus their dispatch through the unified
:data:`AllEvent` discriminated union. The shape mirrors ``test_task.py``
so the conventions stay obvious to future readers:

- happy-path construction
- field validation (length bounds, ``Literal`` enums)
- ``frozen=True`` and ``extra="forbid"`` invariants
- round-trip through ``AllEvent``'s ``TypeAdapter``
- ``AllEvent`` still dispatches to legacy task events

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
    AgentRunEnded,
    AgentRunStarted,
    AllEvent,
    DecisionRecorded,
    HandoffClosed,
    HandoffOpened,
    ItemEnqueued,
    ItemTriaged,
    LockAcquired,
    LockReleased,
    TaskCreated,
    WorkSessionEnded,
    WorkSessionStarted,
)

# Module-level singleton so each test pays the schema-build cost once.
_AllEventAdapter: TypeAdapter[AllEvent] = TypeAdapter(AllEvent)  # pyright: ignore[reportCallIssue]


def _agg() -> str:
    return new_ulid()


# ---- ItemEnqueued ----------------------------------------------------------


def test_item_enqueued_minimal_fields() -> None:
    event = ItemEnqueued(aggregate_id=_agg(), actor="cli:inbox", summary="ping me later")
    assert event.event_type == "inbox.enqueued"
    assert event.schema_version == 1
    assert event.source_ref is None


def test_item_enqueued_rejects_empty_summary() -> None:
    with pytest.raises(PydanticValidationError):
        ItemEnqueued(aggregate_id=_agg(), actor="cli:inbox", summary="")


def test_item_enqueued_rejects_overlong_summary() -> None:
    with pytest.raises(PydanticValidationError):
        ItemEnqueued(aggregate_id=_agg(), actor="cli:inbox", summary="x" * 501)


def test_item_enqueued_accepts_max_summary_and_source_ref() -> None:
    event = ItemEnqueued(
        aggregate_id=_agg(),
        actor="cli:inbox",
        summary="x" * 500,
        source_ref="https://example.com/permalink",
    )
    assert len(event.summary) == 500
    assert event.source_ref == "https://example.com/permalink"


# ---- ItemTriaged -----------------------------------------------------------


@pytest.mark.parametrize("disposition", ["to_task", "decision", "discard"])
def test_item_triaged_accepts_allowed_dispositions(disposition: str) -> None:
    event = ItemTriaged(
        aggregate_id=_agg(),
        actor="cli:triage",
        disposition=disposition,  # type: ignore[arg-type]
    )
    assert event.disposition == disposition


def test_item_triaged_rejects_unknown_disposition() -> None:
    with pytest.raises(PydanticValidationError):
        ItemTriaged(
            aggregate_id=_agg(),
            actor="cli:triage",
            disposition="defer",  # type: ignore[arg-type]
        )


def test_item_triaged_optional_target_and_reason() -> None:
    target = _agg()
    event = ItemTriaged(
        aggregate_id=_agg(),
        actor="cli:triage",
        disposition="to_task",
        target_id=target,
        reason="created task",
    )
    assert event.target_id == target
    assert event.reason == "created task"


# ---- DecisionRecorded ------------------------------------------------------


def test_decision_recorded_minimal_fields() -> None:
    event = DecisionRecorded(aggregate_id=_agg(), actor="cli:decide", text="ship it")
    assert event.event_type == "decision.recorded"
    assert event.context is None


def test_decision_recorded_rejects_empty_text() -> None:
    with pytest.raises(PydanticValidationError):
        DecisionRecorded(aggregate_id=_agg(), actor="cli:decide", text="")


def test_decision_recorded_rejects_overlong_text() -> None:
    with pytest.raises(PydanticValidationError):
        DecisionRecorded(aggregate_id=_agg(), actor="cli:decide", text="x" * 2001)


def test_decision_recorded_accepts_max_text() -> None:
    event = DecisionRecorded(aggregate_id=_agg(), actor="cli:decide", text="x" * 2000)
    assert len(event.text) == 2000


# ---- WorkSessionStarted / Ended --------------------------------------------


def test_work_session_started_optional_scope() -> None:
    event = WorkSessionStarted(aggregate_id=_agg(), actor="agent:claude", scope="phase-2")
    assert event.event_type == "work_session.started"
    assert event.scope == "phase-2"


def test_work_session_ended_optional_summary() -> None:
    event = WorkSessionEnded(aggregate_id=_agg(), actor="agent:claude")
    assert event.event_type == "work_session.ended"
    assert event.summary is None


# ---- AgentRunStarted / Ended -----------------------------------------------


def test_agent_run_started_requires_agent_name() -> None:
    event = AgentRunStarted(
        aggregate_id=_agg(),
        actor="agent:claude",
        agent_name="claude",
        work_session_id=_agg(),
    )
    assert event.event_type == "agent_run.started"
    assert event.agent_name == "claude"


def test_agent_run_started_work_session_id_optional() -> None:
    event = AgentRunStarted(aggregate_id=_agg(), actor="agent:codex", agent_name="codex")
    assert event.work_session_id is None


def test_agent_run_ended_optional_summary() -> None:
    event = AgentRunEnded(aggregate_id=_agg(), actor="agent:codex", summary="finished")
    assert event.summary == "finished"


# ---- LockAcquired / Released -----------------------------------------------


@pytest.mark.parametrize("scope_type", ["task", "project", "global"])
def test_lock_acquired_accepts_each_scope_type(scope_type: str) -> None:
    event = LockAcquired(
        aggregate_id=_agg(),
        actor="agent:claude",
        scope_type=scope_type,  # type: ignore[arg-type]
        scope_id="" if scope_type == "global" else _agg(),
    )
    assert event.scope_type == scope_type


def test_lock_acquired_rejects_unknown_scope_type() -> None:
    with pytest.raises(PydanticValidationError):
        LockAcquired(
            aggregate_id=_agg(),
            actor="agent:claude",
            scope_type="workspace",  # type: ignore[arg-type]
            scope_id=_agg(),
        )


def test_lock_released_requires_lock_id() -> None:
    lock_id = _agg()
    event = LockReleased(aggregate_id=lock_id, actor="agent:claude", lock_id=lock_id)
    assert event.lock_id == lock_id


# ---- HandoffOpened / Closed ------------------------------------------------


def test_handoff_opened_minimal_fields() -> None:
    event = HandoffOpened(
        aggregate_id=_agg(),
        actor="cli:handoff",
        from_actor="alice",
        to_actor="bob",
        topic="please review PR #42",
    )
    assert event.event_type == "handoff.opened"


def test_handoff_opened_rejects_empty_topic() -> None:
    with pytest.raises(PydanticValidationError):
        HandoffOpened(
            aggregate_id=_agg(),
            actor="cli:handoff",
            from_actor="alice",
            to_actor="bob",
            topic="",
        )


def test_handoff_opened_rejects_overlong_topic() -> None:
    with pytest.raises(PydanticValidationError):
        HandoffOpened(
            aggregate_id=_agg(),
            actor="cli:handoff",
            from_actor="alice",
            to_actor="bob",
            topic="x" * 201,
        )


def test_handoff_closed_optional_note() -> None:
    event = HandoffClosed(aggregate_id=_agg(), actor="cli:handoff")
    assert event.note is None


# ---- frozen / extra=forbid -------------------------------------------------


def test_phase2_event_is_frozen() -> None:
    event = ItemEnqueued(aggregate_id=_agg(), actor="cli:inbox", summary="x")
    with pytest.raises(PydanticValidationError):
        event.summary = "y"


def test_phase2_event_forbids_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        DecisionRecorded.model_validate(
            {
                "aggregate_id": _agg(),
                "actor": "cli:decide",
                "text": "ship it",
                "unexpected": "boom",
            }
        )


# ---- AllEvent dispatch for Phase 2 event types ----------------------------


_PHASE2_FACTORIES: list[tuple[str, Any]] = [
    (
        "inbox.enqueued",
        lambda: ItemEnqueued(aggregate_id=_agg(), actor="cli:inbox", summary="x"),
    ),
    (
        "inbox.triaged",
        lambda: ItemTriaged(aggregate_id=_agg(), actor="cli:triage", disposition="discard"),
    ),
    (
        "decision.recorded",
        lambda: DecisionRecorded(aggregate_id=_agg(), actor="cli:decide", text="x"),
    ),
    (
        "work_session.started",
        lambda: WorkSessionStarted(aggregate_id=_agg(), actor="agent:claude"),
    ),
    (
        "work_session.ended",
        lambda: WorkSessionEnded(aggregate_id=_agg(), actor="agent:claude"),
    ),
    (
        "agent_run.started",
        lambda: AgentRunStarted(aggregate_id=_agg(), actor="agent:claude", agent_name="claude"),
    ),
    (
        "agent_run.ended",
        lambda: AgentRunEnded(aggregate_id=_agg(), actor="agent:claude"),
    ),
    (
        "lock.acquired",
        lambda: LockAcquired(
            aggregate_id=_agg(),
            actor="agent:claude",
            scope_type="task",
            scope_id=_agg(),
        ),
    ),
    (
        "lock.released",
        lambda: LockReleased(aggregate_id=_agg(), actor="agent:claude", lock_id=_agg()),
    ),
    (
        "handoff.opened",
        lambda: HandoffOpened(
            aggregate_id=_agg(),
            actor="cli:handoff",
            from_actor="alice",
            to_actor="bob",
            topic="t",
        ),
    ),
    (
        "handoff.closed",
        lambda: HandoffClosed(aggregate_id=_agg(), actor="cli:handoff"),
    ),
]


@pytest.mark.parametrize(
    ("event_type", "factory"),
    _PHASE2_FACTORIES,
    ids=[event_type for event_type, _ in _PHASE2_FACTORIES],
)
def test_phase2_event_roundtrip_via_model_dump(event_type: str, factory: Any) -> None:
    event = factory()
    assert event.event_type == event_type
    restored = _AllEventAdapter.validate_python(event.model_dump(mode="json"))
    assert restored == event
    assert type(restored) is type(event)


# ---- AllEvent extension ----------------------------------------------------


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
    """Forwards-compat: ``AllEvent`` must decode every Phase 2 event type."""
    payload = {
        "event_type": "inbox.enqueued",
        "aggregate_id": _agg(),
        "actor": "cli:inbox",
        "summary": "from all-event",
    }
    event = _AllEventAdapter.validate_python(payload)
    assert isinstance(event, ItemEnqueued)


def test_all_event_rejects_unknown_event_type() -> None:
    payload = {
        "event_type": "phase42.future",
        "aggregate_id": _agg(),
        "actor": "cli:future",
    }
    with pytest.raises(PydanticValidationError):
        _AllEventAdapter.validate_python(payload)
