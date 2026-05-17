"""Tests for ``opshub.connectors.base``.

The Protocol surface and ``SyncResult`` value object are tiny — the
tests here pin the shape so future contributors do not accidentally
loosen the contract (e.g. by dropping ``runtime_checkable`` or making
``SyncResult`` mutable).
"""

from __future__ import annotations

import dataclasses

import pytest

from opshub.connectors.base import Connector, SyncResult


class _StubConnector:
    """Minimal duck-typed connector for Protocol structural checks."""

    name = "stub"

    def sync(self, context: object) -> SyncResult:  # pragma: no cover - test stub
        del context
        return SyncResult(observed_count=0, new_cursor=None)


def test_connector_protocol_is_runtime_checkable() -> None:
    """``isinstance`` against ``Connector`` works at runtime.

    The CLI driver relies on this (defensive check when a non-connector
    slips into the registry). Losing ``runtime_checkable`` would
    silently break that guard.
    """
    assert isinstance(_StubConnector(), Connector)


def test_non_connector_is_rejected_by_runtime_checkable_protocol() -> None:
    """An object missing ``sync``/``name`` is not a :class:`Connector`."""

    class _NotAConnector:
        pass

    assert not isinstance(_NotAConnector(), Connector)


def test_sync_result_is_frozen() -> None:
    """``SyncResult`` is immutable — direct mutation raises."""
    result = SyncResult(observed_count=3, new_cursor="2026-05-17T00:00:00Z")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.observed_count = 4  # type: ignore[misc]


def test_sync_result_is_slotted_no_dynamic_attrs() -> None:
    """``SyncResult`` uses ``__slots__`` so unknown attributes raise.

    The dataclass declares ``slots=True``; this pins the behaviour so a
    refactor that drops the option is caught immediately. ``frozen=True``
    + ``slots=True`` together cause CPython to raise either
    :class:`AttributeError`, :class:`TypeError` (from the synthesised
    ``__setattr__`` super() lookup) or
    :class:`~dataclasses.FrozenInstanceError`, depending on Python
    version, so the test accepts any of them.
    """
    result = SyncResult(observed_count=0, new_cursor=None)
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        result.extra = "nope"  # type: ignore[attr-defined]


def test_sync_result_accepts_none_cursor() -> None:
    """``new_cursor`` is ``str | None`` — both shapes round-trip."""
    none_result = SyncResult(observed_count=0, new_cursor=None)
    str_result = SyncResult(observed_count=2, new_cursor="2026-05-17")
    assert none_result.new_cursor is None
    assert str_result.new_cursor == "2026-05-17"
