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
    _build_source_observed,  # pyright: ignore[reportPrivateUsage]
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
    shared: bool = False,
    last_modifying_user_email: str = "",
    last_modifying_user_display_name: str = "",
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
        shared=shared,
        last_modifying_user_email=last_modifying_user_email,
        last_modifying_user_display_name=last_modifying_user_display_name,
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
    """G2 / G3 / G4 interface contract: tuple shape + order pinned.

    G2 (#276) published the canonical tuple in
    :mod:`opshub.core.document_extract`; G3 (#277) carried a local
    stub of the same name; G4 (#278) deleted the stub and re-exports
    the canonical tuple from the mapper. The canonical order is
    ``(doc, slides, sheets)`` per ADR-0025 §決定 (j) Table 1 — keeping
    this pin here makes any future re-ordering surface as a CI break
    that forces operators to acknowledge the move explicitly.
    """
    assert GOOGLE_WORKSPACE_SOURCE_TYPES == (
        "google_doc",
        "google_slides",
        "google_sheets",
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


# ----- G4 body + provenance + metadata -----------------------------------


def test_map_drive_item_default_body_is_none() -> None:
    """No ``body`` kwarg → ``body=None`` (G3 metadata-only invariant).

    G4 makes ``body`` opt-in via the connector's ``content_extraction``
    flag; callers that omit the kwarg preserve the G3 default-off
    behaviour bit-for-bit so unrelated tests stay valid.
    """
    event = map_drive_item(_raw())
    assert event.body is None


def test_map_drive_item_body_threads_through() -> None:
    """When the connector passes ``body=...`` the event carries it verbatim.

    The mapper does **not** re-truncate or reformat the body — that's
    the extractor's job (ADR-0025 §決定 (b-2)). The mapper just
    forwards what it was handed.
    """
    body = "# heading\n\nthis is the extracted markdown body"
    event = map_drive_item(_raw(), body=body)
    assert event.body == body
    # Provenance stamps stay external / untrusted for the SaaS family
    # — same as the body-less metadata-only path.
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


def test_map_drive_item_empty_body_stays_empty_not_none() -> None:
    """An empty body (legit empty Google Doc) stays as ``""``, not coerced to ``None``.

    :func:`opshub.core.document_extract.extract_workspace_export`
    short-circuits empty exports to ``body=""`` (the file was
    successfully exported, it just had no content). Surfacing that
    distinction matters because the downstream consumer can tell
    "extraction succeeded with no content" from "extraction was
    skipped or failed".
    """
    event = map_drive_item(_raw(), body="")
    assert event.body == ""


def test_map_drive_item_marks_shared() -> None:
    """``shared=True`` (but not shared-with-me) surfaces as ``[shared]``."""
    event = map_drive_item(_raw(shared=True))
    assert event.summary is not None
    assert "[shared]" in event.summary


def test_map_drive_item_shared_with_me_wins_over_shared() -> None:
    """When both ``is_shared_with_me`` and ``shared`` are set, the more specific marker wins.

    ``[shared with me]`` conveys "I received this" which strictly
    implies ``shared=True`` (someone owns it and shared it with me).
    Surfacing both would be redundant noise inside the 200-char cap.
    """
    event = map_drive_item(_raw(shared=True, is_shared_with_me=True))
    assert event.summary is not None
    assert "[shared with me]" in event.summary
    assert "[shared]" not in event.summary


def test_map_drive_item_marks_editor_when_different_from_owner() -> None:
    """``lastModifyingUser`` distinct from the owner gets an ``[edited by ...]`` marker."""
    event = map_drive_item(
        _raw(
            owner_display_name="Alice",
            last_modifying_user_display_name="Bob",
            last_modifying_user_email="bob@example.com",
        )
    )
    assert event.summary is not None
    assert "[edited by Bob]" in event.summary


def test_map_drive_item_omits_editor_marker_when_matches_owner() -> None:
    """``lastModifyingUser`` equal to the owner → no ``[edited by ...]`` marker.

    Noise-reduction guard: the most common shape is "owner == last
    editor" and stamping the marker every time would burn through
    the 200-char summary cap for no signal gain.
    """
    event = map_drive_item(
        _raw(
            owner_display_name="Alice",
            last_modifying_user_display_name="Alice",
        )
    )
    assert event.summary is not None
    assert "[edited by" not in event.summary


def test_map_drive_item_omits_editor_marker_when_drive_omitted_field() -> None:
    """Anonymous / system edits omit ``lastModifyingUser`` → no marker."""
    event = map_drive_item(_raw(last_modifying_user_display_name=""))
    assert event.summary is not None
    assert "[edited by" not in event.summary


def test_map_drive_item_preserves_web_view_link_in_url() -> None:
    """``webViewLink`` lands on ``event.url`` — the Google Doc URL the secretary surfaces.

    The Phase 13 plan's find-document story requires the secretary
    to quote a clickable URL pointing back at the Doc / Sheet /
    Slides. ``url`` is the canonical field that carries that.
    """
    event = map_drive_item(
        _raw(web_url="https://docs.google.com/document/d/F1/edit"),
    )
    assert event.url == "https://docs.google.com/document/d/F1/edit"


# ----- Phase 13 audit Cluster C — A#4 retain-with-marker pins ------------
#
# ADR-0020 retain-everything: trashed / removed / shared-with-me items
# still emit :class:`SourceObserved` (so the projection row carries the
# "this happened" signal); the marker in the summary is the cue
# downstream consumers use to distinguish the lifecycle state. The
# Phase 11 audit Cluster B established the "retain-with-marker" pin
# pattern (cf. ``test_phase11_office_lifecycle``); these tests extend
# the same shape to Phase 13's Drive-side lifecycle markers. Issue
# #288 Cluster C table A#4.


def test_mapped_trashed_item_carries_trashed_marker() -> None:
    """A trashed file round-trips to a :class:`SourceObserved` with the ``[trashed]`` marker.

    Drive surfaces ``trashed=true`` for files the operator (or someone
    with edit access) moved to the trash. ADR-0020 retains them as
    ``trashed``-equivalent rather than deleting the projection row;
    the secretary uses the marker to scope find-document / recall
    answers (a "trashed but recoverable" file is still useful context
    for "did I work on that?" queries). Pinning the full
    :class:`SourceObserved` shape here means the connector tests above
    can rely on the summary marker as a stable contract.
    """
    event = map_drive_item(_raw(file_id="F-trash", trashed=True))

    # The event is emitted at all (no ConnectorFailedError) — ADR-0020
    # retain-everything: trashed files still flow into the projection.
    assert event.external_id == "F-trash"
    # Lifecycle marker present in the summary so downstream consumers
    # can filter trashed-vs-live without re-reading raw.
    assert event.summary is not None
    assert "[trashed]" in event.summary
    # The title is preserved verbatim (the file still has a name
    # while it's in the trash, unlike permanent-delete which clears it).
    assert event.title == "Hello"


def test_mapped_removed_item_retains_with_removed_marker() -> None:
    """A permanently-deleted file round-trips with a placeholder title + ``[removed]`` marker.

    Drive's ``changes.list`` reports permanent deletes as
    ``removed=true`` with the ``file`` object absent (the API stripped
    the metadata because the file no longer exists). The mapper
    synthesises a ``[removed: <fileId>]`` placeholder title so the
    projection row stays well-formed (SourceObserved.title requires
    ``min_length=1``) and stamps the ``[removed]`` summary marker so
    operators can answer "did this file ever exist?" without keeping a
    deleted-files audit log. Sibling pin to the trashed marker — same
    ADR-0020 retain-everything reasoning.
    """
    event = map_drive_item(_raw(file_id="F-gone", name="", removed=True))

    assert event.external_id == "F-gone"
    assert event.title == "[removed: F-gone]"
    assert event.summary is not None
    assert "[removed]" in event.summary
    # ``removed`` is more specific than ``trashed`` so the trashed
    # marker must NOT also appear (operator-facing noise reduction;
    # cf. mapper._build_summary's ``elif`` chain).
    assert "[trashed]" not in event.summary


def test_mapped_shared_with_me_item_carries_marker() -> None:
    """A Shared-with-me file round-trips with the ``[shared with me]`` marker.

    Drive's ``sharedWithMeTime`` (lifted to ``RawDriveItem.is_shared_with_me``)
    distinguishes files the operator received from files they own.
    The secretary's find-document story benefits from the distinction
    — "the spec Alice shared with me last week" vs "the spec I drafted
    myself" route to different mental buckets. Pinning the marker here
    means a future mapper refactor cannot silently drop it without
    surfacing the regression in CI.
    """
    event = map_drive_item(_raw(file_id="F-shared", is_shared_with_me=True))

    assert event.external_id == "F-shared"
    assert event.summary is not None
    assert "[shared with me]" in event.summary
    # ADR-0020 retain-everything: shared items flow through the same
    # projection path as owned items — only the marker differs.
    assert event.body is None  # G3 metadata-only invariant preserved


# ----- Phase 13 audit Cluster C — C#23 defensive code pins ---------------
#
# ``client._normalise_change`` carries defensive branches for two
# Drive payload-shape edge cases that surface in real-world syncs but
# are easy to miss in fixture-based tests:
#
# * ``owners=[]`` — Drive omits the owners list on some Shared Drive
#   items (the shared drive itself owns the file; no individual user
#   owner) and on some drive-level events (rename / permission grant).
#   ``client.py`` defaults ``owner_email`` / ``owner_display_name`` to
#   ``""`` so the mapper's downstream code never hits an IndexError.
# * ``webViewLink`` missing — Drive omits it on permanent-delete
#   change records (the file no longer has a public URL) and on some
#   folder-shaped change events. ``client.py`` defaults
#   ``web_view_link`` to ``""``; the mapper normalises that to
#   ``url=None`` on the :class:`SourceObserved`.
#
# Pinning the mapper-side behaviour for those defensive-defaulted
# shapes guarantees the client.py defensive code is not dead code —
# a future change that re-shapes ``RawDriveItem`` (e.g. switching to
# Optional fields) would surface here as a test break. Issue #288
# Cluster C table C#23.


def test_handles_owners_empty() -> None:
    """A :class:`RawDriveItem` with ``owners=[]`` defaults round-trips cleanly.

    Drive omits the owners list on Shared Drive items (where the
    drive owns the file, not an individual user) and on some
    drive-level change events. ``client._normalise_change`` defaults
    ``owner_email`` and ``owner_display_name`` to ``""``; this test
    pins the mapper-side behaviour so the defensive code path stays
    exercised end-to-end. Without this pin a future refactor that
    drops the ``isinstance(owners_obj, list) and owners_obj`` guard
    would silently break Shared Drive ingest.
    """
    event = map_drive_item(_raw(owner_email="", owner_display_name=""))

    # Event still emitted — no ConnectorFailedError, no Pydantic
    # validation crash. The summary degrades to just the mimeType (no
    # owner parts to render) but is still non-empty so the projection
    # row carries a useful hint.
    assert event.external_id == "F1"
    assert event.summary is not None
    assert event.summary == "application/vnd.google-apps.document"
    # Provenance stamps unchanged — the SaaS family invariant
    # (external / untrusted) does not depend on owner identity.
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


def test_handles_web_view_link_missing() -> None:
    """A :class:`RawDriveItem` with ``web_view_link=""`` becomes ``event.url=None``.

    Drive omits ``webViewLink`` on permanent-delete change records and
    on some folder-shaped change events. ``client._normalise_change``
    defaults ``web_view_link`` to ``""``; the mapper normalises that
    to ``None`` on the :class:`SourceObserved` so the projection's
    nullable ``url`` column is the canonical "no link available"
    signal (not an empty string, which would be harder to filter on
    downstream). Sibling pin to :func:`test_map_drive_item_empty_url_becomes_none`
    framed at the C#23 defensive-code-stays-live angle.
    """
    event = map_drive_item(_raw(file_id="F-no-url", web_url=""))

    assert event.external_id == "F-no-url"
    assert event.url is None
    # Other fields still populated — the URL is the only thing that
    # degrades on a missing webViewLink.
    assert event.title == "Hello"
    assert event.summary is not None


def test_build_source_observed_whitespace_only_summary_normalises_to_none() -> None:
    """Issue #343: whitespace-only summary input collapses to ``None``.

    The naturally composed Workspace summary always carries the
    mimeType suffix (``"<owner> (<email>) — <mime>"`` or just
    ``"<mime>"``) so a whitespace-only candidate is not currently
    reachable via :func:`map_drive_item`. This test exercises
    :func:`_build_source_observed` directly to pin the SSOT helper
    wiring: even if a future change to ``_build_summary`` could emit a
    whitespace candidate, the :func:`normalise_optional_text` helper
    keeps the ``sources.summary`` column free of visually-empty
    previews (matches the Slack / Teams / MS365 / Gmail / Calendar /
    GitHub-notification mappers).
    """
    event = _build_source_observed(
        external_id="F-ws",
        source_type=GENERIC_FILE_SOURCE_TYPE,
        title="title",
        url="https://example.com/",
        summary="   \n\t  ",
        occurred_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
        actor=DEFAULT_ACTOR,
        body=None,
    )
    assert event.summary is None
