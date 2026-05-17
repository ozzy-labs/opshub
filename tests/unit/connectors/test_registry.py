"""Tests for ``opshub.connectors._registry``.

The registry is a process-wide dict; every test calls
:func:`unregister_all` through the autouse fixture so the global state
does not leak between tests.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from opshub.connectors import (
    SyncResult,
    discover_connectors,
    register_connector,
    unregister_all,
)


class _StubConnector:
    """Duck-typed connector used by the registry tests."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name

    def sync(self, context: object) -> SyncResult:  # pragma: no cover - test stub
        del context
        return SyncResult(observed_count=0, new_cursor=None)


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Clear the process-wide registry before every test.

    The registry is module-level state; without this fixture, tests
    that run in the same process would observe each other's
    registrations and fail non-deterministically.
    """
    unregister_all()
    yield
    unregister_all()


def test_register_then_discover_returns_instance() -> None:
    stub = _StubConnector()
    register_connector(stub)
    assert discover_connectors() == [stub]


def test_register_same_instance_twice_is_idempotent() -> None:
    """Re-registering the **same** instance is a silent no-op.

    Without idempotency, reload-heavy test harnesses that re-import a
    connector module would raise on the second pass.
    """
    stub = _StubConnector()
    register_connector(stub)
    register_connector(stub)
    assert discover_connectors() == [stub]


def test_register_different_instance_with_same_name_raises() -> None:
    """Two competing implementations under one name must fail loudly."""
    first = _StubConnector(name="github")
    second = _StubConnector(name="github")
    register_connector(first)
    with pytest.raises(ValueError, match="github"):
        register_connector(second)
    # First registration survives the failed second registration.
    assert discover_connectors() == [first]


def test_unregister_all_empties_registry() -> None:
    register_connector(_StubConnector(name="a"))
    register_connector(_StubConnector(name="b"))
    assert len(discover_connectors()) == 2
    unregister_all()
    assert discover_connectors() == []


def test_discover_returns_empty_list_when_nothing_registered() -> None:
    """Fresh process / fully-reset registry yields ``[]`` (Phase 3 MVP)."""
    assert discover_connectors() == []
