"""Tests for ``opshub.connectors.google_mail.cursor`` (Phase 14 G3).

Pinning the cursor key string here keeps the projection-side
``connector_cursors`` row stable across refactors. The shape mirrors
:mod:`opshub.connectors.google_workspace.cursor` (single cursor per
connector, key is a colon-separated namespace).
"""

from __future__ import annotations

from opshub.connectors.google_mail.cursor import CURSOR_HISTORY


def test_cursor_history_key_pin() -> None:
    """``CURSOR_HISTORY`` is the single Gmail cursor key.

    Changing this string is a forward-incompatible projection change
    (existing operator cursors live under the old name); pin it so a
    rename surfaces as an explicit failure.
    """
    assert CURSOR_HISTORY == "google_mail:history"
