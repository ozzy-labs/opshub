"""Tests for :class:`opshub.services.inbox_service.InboxService`."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Literal, cast

import pytest
from pydantic import ValidationError as PydanticValidationError

from opshub.core.errors import ValidationError
from opshub.core.ids import new_ulid
from opshub.domain.events import DomainEvent, ItemEnqueued, ItemTriaged, TaskCreated
from opshub.services.event_store import InMemoryEventStore
from opshub.services.inbox_service import InboxService
from opshub.services.projector import NoOpProjector

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class _RecordingProjector:
    """Projector test double that captures applied events in order."""

    def __init__(self) -> None:
        self.applied: list[DomainEvent] = []

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = connection
        self.applied.append(event)


# ---- enqueue --------------------------------------------------------------


def test_enqueue_appends_item_enqueued_event() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = InboxService(store=store, projector=projector)

    event = service.enqueue(summary="triage me", source_ref="https://example.com/x")

    assert isinstance(event, ItemEnqueued)
    assert event.summary == "triage me"
    assert event.source_ref == "https://example.com/x"
    assert store.events == [event]
    assert projector.applied == [event]
    # Identity, not just equality.
    assert projector.applied[0] is event


def test_enqueue_rejects_empty_summary() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(PydanticValidationError):
        service.enqueue(summary="")


def test_enqueue_rejects_oversized_summary() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(PydanticValidationError):
        service.enqueue(summary="x" * 501)


def test_enqueue_allows_omitted_source_ref() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    event = service.enqueue(summary="no provenance")
    assert event.source_ref is None


# ---- triage --to-task -----------------------------------------------------


def test_triage_to_task_appends_task_created_then_item_triaged() -> None:
    """``--to-task`` must append both events in order, on the same UoW."""
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = InboxService(store=store, projector=projector)
    item_id = new_ulid()

    event = service.triage(item_id, to_task="write ADR")

    assert isinstance(event, ItemTriaged)
    assert event.aggregate_id == item_id
    assert event.disposition == "to_task"
    assert event.target_id is not None
    # The reason field is unused for to_task; the title lives on the task event.
    assert event.reason is None

    # Store sees both events in order: task.created first, then inbox.triaged.
    assert len(store.events) == 2
    task_event = store.events[0]
    triage_event = store.events[1]
    assert isinstance(task_event, TaskCreated)
    assert task_event.title == "write ADR"
    assert task_event.aggregate_id == event.target_id, (
        "ItemTriaged.target_id must point at the new task's aggregate id"
    )
    assert triage_event is event

    # Projector sees the same sequence.
    assert projector.applied == [task_event, triage_event]


# ---- triage --decision ----------------------------------------------------


def test_triage_decision_records_reason_and_pre_allocates_target_id() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = InboxService(store=store, projector=projector)
    item_id = new_ulid()

    event = service.triage(item_id, decision="needs design review")

    assert isinstance(event, ItemTriaged)
    assert event.aggregate_id == item_id
    assert event.disposition == "decision"
    assert event.target_id is not None
    assert len(event.target_id) == 26, "target_id must be a ULID"
    assert event.reason == "needs design review"

    # Only the single triage event is appended; no DecisionRecorded here
    # (step 4's decision service handles that path).
    assert store.events == [event]
    assert projector.applied == [event]


# ---- triage --discard -----------------------------------------------------


def test_triage_discard_records_reason_and_no_target() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = InboxService(store=store, projector=projector)
    item_id = new_ulid()

    event = service.triage(item_id, discard="duplicate of #42")

    assert isinstance(event, ItemTriaged)
    assert event.aggregate_id == item_id
    assert event.disposition == "discard"
    assert event.target_id is None
    assert event.reason == "duplicate of #42"

    assert store.events == [event]
    assert projector.applied == [event]


# ---- triage validation ----------------------------------------------------


def test_triage_rejects_non_ulid_item_id() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.triage("not-a-ulid", discard="oops")


def test_triage_rejects_no_disposition() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.triage(new_ulid())


def test_triage_rejects_multiple_dispositions() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.triage(new_ulid(), to_task="t", discard="d")


def test_triage_rejects_all_three_dispositions() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(ValidationError):
        service.triage(new_ulid(), to_task="t", decision="d", discard="x")


# ---- actor stamping -------------------------------------------------------


def test_actor_defaults_to_cli_default() -> None:
    service = InboxService(store=InMemoryEventStore(), projector=NoOpProjector())
    event = service.enqueue(summary="default actor")
    assert event.actor == "cli:default"


def test_custom_actor_is_stamped_on_each_event() -> None:
    store = InMemoryEventStore()
    service = InboxService(
        store=store,
        projector=NoOpProjector(),
        actor="agent:capture",
    )

    enqueued = service.enqueue(summary="captured")
    triaged = service.triage(enqueued.aggregate_id, to_task="follow up")

    # The triage event AND the task event it stamps both carry the actor.
    assert enqueued.actor == "agent:capture"
    assert triaged.actor == "agent:capture"
    for stored in store.events:
        assert stored.actor == "agent:capture"


# ---- atomic rollback ------------------------------------------------------


class _FailingProjector:
    """Projector that raises on ``apply`` to exercise rollback paths."""

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = event
        _ = connection
        raise RuntimeError("simulated projector failure")


class _UowSpy:
    """Minimal UoW factory test double that records commit / rollback decisions.

    The factory returns a context manager that yields ``None`` (the
    in-memory event store ignores the connection anyway). On clean
    exit the spy records ``"commit"``; on exception it records
    ``"rollback"`` and re-raises — same semantics as ``engine.begin()``.

    Typed as returning ``Connection`` so the spy can be passed where
    ``uow_factory: Callable[[], ContextManager[Connection]]`` is expected;
    the in-memory store ignores the value at runtime.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.entered = 0

    def __call__(self) -> _UowSpy:
        self.entered += 1
        return self

    def __enter__(self) -> Connection:
        return cast("Connection", None)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        del exc, tb
        self.events.append("rollback" if exc_type is not None else "commit")
        # Do not swallow the exception — propagate to the caller.
        return False


def test_enqueue_rolls_back_uow_when_projector_fails() -> None:
    """A projector failure on enqueue must trigger the UoW rollback path."""
    spy = _UowSpy()
    service = InboxService(
        store=InMemoryEventStore(),
        projector=_FailingProjector(),
        uow_factory=spy,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.enqueue(summary="will not persist")

    assert spy.entered == 1
    assert spy.events == ["rollback"]


def test_triage_to_task_rolls_back_uow_when_projector_fails() -> None:
    """``--to-task`` appends two events: a mid-batch failure must roll the UoW back."""
    spy = _UowSpy()
    service = InboxService(
        store=InMemoryEventStore(),
        projector=_FailingProjector(),
        uow_factory=spy,
    )

    with pytest.raises(RuntimeError, match="simulated projector failure"):
        service.triage(new_ulid(), to_task="will not stick")

    assert spy.entered == 1
    assert spy.events == ["rollback"]


def test_enqueue_commits_uow_on_success() -> None:
    """Happy path: the UoW exits cleanly and we record a commit."""
    spy = _UowSpy()
    service = InboxService(
        store=InMemoryEventStore(),
        projector=NoOpProjector(),
        uow_factory=spy,
    )

    service.enqueue(summary="commits ok")

    assert spy.entered == 1
    assert spy.events == ["commit"]
