"""Tests for :mod:`opshub.services.event_store`."""

from __future__ import annotations

from opshub.core.ids import new_ulid
from opshub.domain.events import TaskCreated
from opshub.services.event_store import EventStore, InMemoryEventStore


def _make_event() -> TaskCreated:
    return TaskCreated(aggregate_id=new_ulid(), actor="cli:test", title="example")


def test_in_memory_event_store_stores_appended_event() -> None:
    store = InMemoryEventStore()
    event = _make_event()

    store.append(event)

    snapshot = store.events
    assert snapshot == [event]
    # Identity check: frozen events are stored directly, no copy.
    assert snapshot[0] is event


def test_in_memory_event_store_returns_defensive_copy() -> None:
    """Mutating the snapshot must not mutate the internal log."""
    store = InMemoryEventStore()
    store.append(_make_event())

    snapshot = store.events
    snapshot.clear()
    snapshot.append(_make_event())

    # Internal log is unchanged: still one event, distinct from the mutated snapshot.
    assert len(store.events) == 1
    assert store.events != snapshot


def test_in_memory_event_store_preserves_append_order() -> None:
    store = InMemoryEventStore()
    events = [_make_event() for _ in range(3)]
    for event in events:
        store.append(event)

    assert store.events == events


def test_in_memory_event_store_conforms_to_protocol() -> None:
    """Runtime ``isinstance`` check against the :class:`EventStore` Protocol."""
    store = InMemoryEventStore()
    assert isinstance(store, EventStore)
