"""Tests for ``opshub.connectors.google_mail.cursor`` (Phase 14 G3).

The cursor module is intentionally a single constant; the test
pins the literal value (callers cross-reference it) and asserts
the connector name component matches the package name so the
``connector_cursors`` projection lookup stays consistent if a
future refactor renames either.
"""

from __future__ import annotations

from opshub.connectors.google_mail.connector import GoogleMailConnector
from opshub.connectors.google_mail.cursor import CURSOR_HISTORY


def test_cursor_history_literal_pinned() -> None:
    """Cursor key value pin.

    ADR-0010 §責務 6 + Phase 14 plan §3 G3 contract: the cursor key
    is operator-invisible but its stability across upgrades is the
    whole point of the projection-based cursor design. Renaming this
    would silently re-bootstrap every operator's mailbox on the next
    sync — fail-loudly here instead.
    """
    assert CURSOR_HISTORY == "google_mail:history"


def test_cursor_key_prefix_matches_connector_name() -> None:
    """The cursor key's prefix matches the connector's registry name.

    The Phase 13 Google Workspace cursor (``google_workspace:changes``)
    + Phase 11 Teams cursor (``teams:...``) follow the same
    convention; this test pins it so future connector authors keep
    the symmetry.
    """
    connector_name = GoogleMailConnector.name
    prefix = CURSOR_HISTORY.split(":", 1)[0]
    assert prefix == connector_name
