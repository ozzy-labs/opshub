"""Tests for :mod:`opshub.services.projector`."""

from __future__ import annotations

from opshub.core.ids import new_ulid
from opshub.domain.events import TaskCreated
from opshub.services.projector import NoOpProjector, Projector


def test_noop_projector_accepts_event_without_error() -> None:
    projector = NoOpProjector()
    event = TaskCreated(aggregate_id=new_ulid(), actor="cli:test", title="example")

    # Must not raise. ``apply`` is declared ``-> None``; calling it with no
    # side effect is the entire contract of the no-op projector.
    projector.apply(event)


def test_noop_projector_conforms_to_protocol() -> None:
    projector = NoOpProjector()
    assert isinstance(projector, Projector)
