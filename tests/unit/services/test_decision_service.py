"""Tests for :class:`opshub.services.decision_service.DecisionService`."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError as PydanticValidationError

from opshub.domain.events import DecisionRecorded, DomainEvent
from opshub.services.decision_service import DecisionService
from opshub.services.event_store import InMemoryEventStore
from opshub.services.projector import NoOpProjector

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.engine import Connection


class _RecordingProjector:
    """Projector test double that captures applied events in order.

    The ``connection`` argument matches the
    :class:`opshub.services.projector.Projector` Protocol — the in-memory
    suite passes ``None`` because there is no SQL transaction to join.
    """

    def __init__(self) -> None:
        self.applied: list[DomainEvent] = []

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = connection
        self.applied.append(event)


class _FailingProjector:
    """Projector double that raises on every ``apply``.

    Used by the atomicity test to assert that ``record_decision`` propagates
    the failure (and that a configured ``uow_factory`` rolls the UoW back).
    """

    class BoomError(RuntimeError):
        """Distinct exception so the assertion catches the right thing."""

    def apply(self, event: DomainEvent, connection: Connection | None = None) -> None:
        _ = event
        _ = connection
        raise _FailingProjector.BoomError("projector failed")


# ---- record_decision ------------------------------------------------------


def test_record_decision_appends_event_with_text_only() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = DecisionService(store=store, projector=projector)

    event = service.record_decision("use python 3.13")

    assert isinstance(event, DecisionRecorded)
    assert event.text == "use python 3.13"
    assert event.context is None
    # The event in the store is the same instance returned to the caller.
    assert store.events == [event]
    # The projector saw the exact same event.
    assert projector.applied == [event]
    assert projector.applied[0] is event


def test_record_decision_accepts_context() -> None:
    store = InMemoryEventStore()
    projector = _RecordingProjector()
    service = DecisionService(store=store, projector=projector)

    event = service.record_decision("merge PR as-is", context="approved in standup")

    assert event.text == "merge PR as-is"
    assert event.context == "approved in standup"


def test_record_decision_generates_unique_ulids() -> None:
    """Each call mints a fresh ULID for ``aggregate_id``."""
    service = DecisionService(store=InMemoryEventStore(), projector=NoOpProjector())

    first = service.record_decision("d1")
    second = service.record_decision("d2")

    assert first.aggregate_id != second.aggregate_id
    assert len(first.aggregate_id) == 26
    assert len(second.aggregate_id) == 26


# ---- text validation ------------------------------------------------------


def test_record_decision_rejects_empty_text() -> None:
    service = DecisionService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(PydanticValidationError):
        service.record_decision("")


def test_record_decision_rejects_text_over_2000_chars() -> None:
    service = DecisionService(store=InMemoryEventStore(), projector=NoOpProjector())
    with pytest.raises(PydanticValidationError):
        service.record_decision("x" * 2001)


def test_record_decision_accepts_text_at_upper_bound() -> None:
    """Exactly 2000 chars is allowed (boundary)."""
    service = DecisionService(store=InMemoryEventStore(), projector=NoOpProjector())
    event = service.record_decision("x" * 2000)
    assert len(event.text) == 2000


# ---- actor stamping -------------------------------------------------------


def test_actor_defaults_to_cli_default() -> None:
    store = InMemoryEventStore()
    service = DecisionService(store=store, projector=NoOpProjector())

    event = service.record_decision("d")

    assert event.actor == "cli:default"


def test_custom_actor_is_stamped() -> None:
    store = InMemoryEventStore()
    service = DecisionService(store=store, projector=NoOpProjector(), actor="agent:planner")

    event = service.record_decision("d")

    assert event.actor == "agent:planner"


# ---- atomicity ------------------------------------------------------------


def test_record_decision_propagates_projector_failure() -> None:
    """A failing projector must surface, not be swallowed by the service."""
    service = DecisionService(
        store=InMemoryEventStore(),
        projector=_FailingProjector(),
    )
    with pytest.raises(_FailingProjector.BoomError):
        service.record_decision("d")


def test_record_decision_rolls_back_uow_on_projector_failure() -> None:
    """When a ``uow_factory`` is configured, a projector failure rolls back the UoW.

    We assert this by tracking ``commit`` / ``rollback`` calls on a fake
    UoW: a failing projector must result in ``rollback`` being called and
    ``commit`` not. The in-memory store / projector do not actually write
    to a DB, but the contract the service implements (single UoW around
    append + apply) is identical to the SQLAlchemy backed wiring.
    """
    rollbacks: list[str] = []
    commits: list[str] = []

    @contextmanager
    def _factory() -> Generator[Connection]:
        try:
            # ``None`` is fine here — the in-memory store / projector
            # ignore the connection argument. We cast it to the protocol
            # type so the generator signature matches what the service
            # asks for (``AbstractContextManager[Connection]``).
            yield cast("Connection", None)
        except BaseException:
            rollbacks.append("rollback")
            raise
        else:
            commits.append("commit")

    uow_factory = cast(
        "Callable[[], AbstractContextManager[Connection]]",
        _factory,
    )

    service = DecisionService(
        store=InMemoryEventStore(),
        projector=_FailingProjector(),
        uow_factory=uow_factory,
    )

    with pytest.raises(_FailingProjector.BoomError):
        service.record_decision("d")

    assert rollbacks == ["rollback"]
    assert commits == []


def test_record_decision_commits_uow_on_success() -> None:
    """Successful command commits the UoW exactly once."""
    rollbacks: list[str] = []
    commits: list[str] = []

    @contextmanager
    def _factory() -> Generator[Connection]:
        try:
            yield cast("Connection", None)
        except BaseException:
            rollbacks.append("rollback")
            raise
        else:
            commits.append("commit")

    uow_factory = cast(
        "Callable[[], AbstractContextManager[Connection]]",
        _factory,
    )

    service = DecisionService(
        store=InMemoryEventStore(),
        projector=NoOpProjector(),
        uow_factory=uow_factory,
    )

    service.record_decision("d")

    assert commits == ["commit"]
    assert rollbacks == []
