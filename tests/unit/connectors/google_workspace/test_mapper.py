"""Tests for ``opshub.connectors.google_workspace.mapper`` (Phase 13 G3).

The mapper is pure-Python (no extras dep) so tests run unconditionally.
Coverage map:

* mimeType → source_type lookup pin (ADR-0025 §決定 (d') 3 native +
  catch-all)
* ``map_drive_item`` builds a :class:`SourceObserved` with the
  expected fields + provenance stamps
* trashed / removed / sharedWithMe markers appear in the summary
* ``body=None`` invariant (G3 ships metadata-only)
* Live items without a name raise :class:`ConnectorFailedError`;
  removed items synthesise a placeholder
* Summary respects the 200-char cap
* :data:`GOOGLE_WORKSPACE_SOURCE_TYPES` pins the 3 native types in
  order (G2 / G3 interface contract — Phase 13 plan §G3)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from opshub.connectors.google_workspace.client import RawDriveItem
from opshub.connectors.google_workspace.mapper import (
    DEFAULT_ACTOR,
    GENERIC_FILE_SOURCE_TYPE,
    GOOGLE_DOC_SOURCE_TYPE,
    GOOGLE_SHEETS_SOURCE_TYPE,
    GOOGLE_SLIDES_SOURCE_TYPE,
    GOOGLE_WORKSPACE_SOURCE_TYPES,
    SUMMARY_MAX_CHARS,
    map_drive_item,
    source_type_for_mime_type,
)
from opshub.core.errors import ConnectorFailedError


def _raw(
    file_id: str = "F1",
    *,
    name: str = "Hello",
    mime_type: str = "application/vnd.google-apps.document",
    modified: str = "2026-05-31T12:00:00Z",
    web_url: str = "https://drive.google.com/file/d/F1/view",
    owner_email: str = "alice@example.com",
    owner_display_name: str = "Alice",
    removed: bool = False,
    trashed: bool = False,
    is_shared_with_me: bool = False,
    drive_id: str = "",
    raw: dict[str, Any] | None = None,
) -> RawDriveItem:
    """Factory for :class:`RawDriveItem` fixtures (cuts boilerplate)."""
    return RawDriveItem(
        file_id=file_id,
        removed=removed,
        trashed=trashed,
        name=name,
        mime_type=mime_type,
        modified_time_iso=modified,
        web_view_link=web_url,
        owner_email=owner_email,
        owner_display_name=owner_display_name,
        is_shared_with_me=is_shared_with_me,
        drive_id=drive_id,
        raw=raw or {},
    )


# ----- mimeType → source_type --------------------------------------------


def test_source_type_pins_3_native_types() -> None:
    """ADR-0025 §決定 (d') mimeType ↔ source_type lookup.

    The three Workspace native mimeTypes are the find-document
    targets; pinning the mapping here prevents silent string drift if
    Google ever renames a mimeType (in which case the test catches the
    issue and forces the operator to acknowledge the change explicitly).
    """
    assert (
        source_type_for_mime_type("application/vnd.google-apps.document") == GOOGLE_DOC_SOURCE_TYPE
    )
    assert (
        source_type_for_mime_type("application/vnd.google-apps.spreadsheet")
        == GOOGLE_SHEETS_SOURCE_TYPE
    )
    assert (
        source_type_for_mime_type("application/vnd.google-apps.presentation")
        == GOOGLE_SLIDES_SOURCE_TYPE
    )


def test_source_type_falls_back_to_generic() -> None:
    """Unknown mimeTypes route to the catch-all so coverage stays complete."""
    assert source_type_for_mime_type("application/pdf") == GENERIC_FILE_SOURCE_TYPE
    assert (
        source_type_for_mime_type("application/vnd.google-apps.folder") == GENERIC_FILE_SOURCE_TYPE
    )
    assert source_type_for_mime_type("") == GENERIC_FILE_SOURCE_TYPE


def test_google_workspace_source_types_pin_tuple() -> None:
    """G2 / G3 interface contract: tuple shape + order pinned.

    Phase 13 plan §G3 wave-2 parallel implementation note: this tuple
    is the import target G2 (#276) will publish from
    :mod:`opshub.domain.events.source`. When the G2 PR merges and
    swaps the import in, the *order* and *values* must match
    bit-for-bit — this test makes that contract visible.
    """
    assert GOOGLE_WORKSPACE_SOURCE_TYPES == (
        "google_doc",
        "google_sheets",
        "google_slides",
    )


# ----- map_drive_item ----------------------------------------------------


def test_map_drive_item_builds_source_observed() -> None:
    event = map_drive_item(_raw())
    assert event.connector_name == "google_workspace"
    assert event.external_id == "F1"
    assert event.source_type == GOOGLE_DOC_SOURCE_TYPE
    assert event.title == "Hello"
    assert event.url == "https://drive.google.com/file/d/F1/view"
    assert event.summary is not None
    assert event.body is None  # G3 ships metadata-only; G4 fills body
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"
    assert event.actor == DEFAULT_ACTOR


def test_map_drive_item_parses_modified_time() -> None:
    event = map_drive_item(_raw(modified="2026-05-31T12:34:56Z"))
    assert event.occurred_at == datetime(2026, 5, 31, 12, 34, 56, tzinfo=UTC)


def test_map_drive_item_routes_sheets() -> None:
    event = map_drive_item(_raw(mime_type="application/vnd.google-apps.spreadsheet"))
    assert event.source_type == GOOGLE_SHEETS_SOURCE_TYPE


def test_map_drive_item_routes_slides() -> None:
    event = map_drive_item(_raw(mime_type="application/vnd.google-apps.presentation"))
    assert event.source_type == GOOGLE_SLIDES_SOURCE_TYPE


def test_map_drive_item_routes_unknown_to_generic() -> None:
    event = map_drive_item(_raw(mime_type="application/pdf"))
    assert event.source_type == GENERIC_FILE_SOURCE_TYPE


# ----- summary markers ---------------------------------------------------


def test_map_drive_item_marks_trashed() -> None:
    event = map_drive_item(_raw(trashed=True))
    assert event.summary is not None
    assert "[trashed]" in event.summary


def test_map_drive_item_marks_removed() -> None:
    """Removed items synthesise a placeholder title + ``[removed]`` marker.

    ADR-0020 retain-everything: permanent deletes still emit a
    :class:`SourceObserved`, just with a placeholder so the projection
    row carries the "this used to exist" signal.
    """
    event = map_drive_item(_raw(name="", removed=True))
    assert event.title == "[removed: F1]"
    assert event.summary is not None
    assert "[removed]" in event.summary


def test_map_drive_item_marks_shared_with_me() -> None:
    event = map_drive_item(_raw(is_shared_with_me=True))
    assert event.summary is not None
    assert "[shared with me]" in event.summary


def test_map_drive_item_rejects_empty_file_id() -> None:
    raw = _raw(file_id="")
    with pytest.raises(ConnectorFailedError, match="no file_id"):
        map_drive_item(raw)


def test_map_drive_item_rejects_live_item_without_title() -> None:
    """Live items must carry a title (SourceObserved.title min_length=1)."""
    raw = _raw(name="", removed=False)
    with pytest.raises(ConnectorFailedError, match="no title"):
        map_drive_item(raw)


def test_map_drive_item_summary_respects_cap() -> None:
    """Summary is bounded at :data:`SUMMARY_MAX_CHARS` (ADR-0005)."""
    long_name = "a" * 500
    event = map_drive_item(
        _raw(
            name="x",
            owner_display_name=long_name,
            owner_email="b@example.com",
        )
    )
    assert event.summary is not None
    assert len(event.summary) <= SUMMARY_MAX_CHARS


def test_map_drive_item_empty_url_becomes_none() -> None:
    """Empty URL normalises to ``None`` rather than empty string."""
    event = map_drive_item(_raw(web_url=""))
    assert event.url is None


def test_map_drive_item_actor_override() -> None:
    """Custom actor is forwarded onto the event."""
    event = map_drive_item(_raw(), actor="connector:google_workspace:test")
    assert event.actor == "connector:google_workspace:test"
