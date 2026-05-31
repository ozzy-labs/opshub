"""Tests for ``opshub.connectors.google_calendar.cursor``.

Cursor module is a single module-level constant — the tests here pin
the literal value and the fact that the constant lives at the
documented import path so a future rename surfaces immediately in
review.

The pin matters because the cursor key is the foreign key the
``connector_cursors`` projection upserts on. A silent rename would
orphan every operator's stored sync token and force them to re-bootstrap
on the next sync (which, while recoverable via the fallback path, is
operator-visible churn).
"""

from __future__ import annotations

from opshub.connectors.google_calendar.cursor import CURSOR_EVENTS


def test_cursor_events_pin() -> None:
    """``CURSOR_EVENTS`` is the namespaced cursor key for ``events.list``.

    The literal value is stored in the ``connector_cursors``
    projection and replayed verbatim as the ``syncToken`` query
    parameter — drift here would orphan existing cursors.
    """
    assert CURSOR_EVENTS == "google_calendar:events"


def test_cursor_module_exports() -> None:
    """``__all__`` lists exactly the public cursor name.

    Pinning ``__all__`` keeps a future helper added to the module
    from being surfaced as a public API by accident.
    """
    import opshub.connectors.google_calendar.cursor as cursor_module

    assert cursor_module.__all__ == ["CURSOR_EVENTS"]
