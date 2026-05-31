"""Phase 13 audit Cluster C — Google Workspace export round-trip integration test.

Plan §G5 DoD pin: end-to-end exercise of the Phase 13 G3 + G4 export
path with the **Phase 13-specific** fixture binaries under
``tests/fixtures/google_workspace/`` rather than the Phase 11 Office
fixtures that ``test_document_extract.py`` re-uses. Issue #288 Cluster
C C#14 (must) — fixture 死蔵解決.

Why this test exists
--------------------

The Phase 13 plan §G5 closeout listed "Phase 13 専用 e2e (extract
round-trip)" as a DoD line item and committed three dedicated fixture
files (``exported_doc.docx`` / ``exported_sheet.xlsx`` /
``exported_slides.pptx``) so the test would be independent of the
Phase 11 Office fixtures (``sample.docx`` etc.). The G5 closeout
landed but the test never did; the fixture files sat dead in
``tests/fixtures/google_workspace/`` without a single ``rg`` hit
across ``tests/``. The Phase 13 audit (issue #288, Cluster C) caught
the gap; this test closes it.

The test pattern mirrors the Phase 11 ``test_phase11_office_lifecycle``
representational pattern (`fixture binary → mocked Drive API export →
markitdown → body persist`) but stays narrower in scope: it exercises
only the export-extract-mapper-projection round-trip for each of the
three Workspace native source_types, not the full MCP / search /
embedding lifecycle (which the broader
:mod:`tests.integration.test_phase13_google_workspace_lifecycle`
already pins).

What this pins
--------------

For each of the three Phase 13 Workspace native ``source_type``
discriminators (``google_doc`` / ``google_slides`` / ``google_sheets``):

1. **Drive API mock** — :class:`httpx.MockTransport` routes a
   ``files.export(fileId, mimeType=<MS Office mediatype>)`` call to a
   handler that returns the Phase 13 fixture binary.
2. **markitdown round-trip** — :func:`extract_workspace_export`
   writes the bytes to a tempfile, dispatches markitdown on the
   suffix, and returns an :class:`ExtractResult` carrying the
   extracted markdown body.
3. **body persist** — the resulting body lands on the
   :class:`SourceObserved` event via :func:`map_drive_item` with the
   Phase 13 source_type discriminator and the SaaS family provenance
   tags (``external`` / ``untrusted``).
4. **fixture activation** — ``tests/fixtures/google_workspace/
   exported_doc.docx`` / ``.xlsx`` / ``.pptx`` are read explicitly so
   ``rg -n 'exported_doc.docx' tests/`` hits this file.

The test does **not** exercise the full connector ``sync`` path
because the Phase 13 audit explicitly scoped that to other tests
(``test_connector.py::test_sync_content_extraction_*``). The narrow
focus here is the **export → extract → mapper round-trip** the
Phase 13 plan §G5 DoD called out as missing.

The Phase 13 connectors tests skip when ``[connectors-google-workspace]``
extras are missing; the same gate applies here so the suite stays
green on environments without ``httpx`` installed (Phase 11 / 13
``importorskip`` precedent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip(
    "httpx",
    reason="Phase 13 Google Workspace round-trip test requires the "
    "'connectors-google-workspace' extras",
)
pytest.importorskip(
    "markitdown",
    reason="Phase 13 round-trip test requires the '[office]' extras for the "
    "markitdown body extractor",
)

import httpx

# Phase 14 G2 (#294): shared OAuth helper lives under google_auth now.
from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
from opshub.connectors.google_workspace.client import DRIVE_API_BASE, DriveClient
from opshub.connectors.google_workspace.mapper import (
    GOOGLE_DOC_SOURCE_TYPE,
    GOOGLE_SHEETS_SOURCE_TYPE,
    GOOGLE_SLIDES_SOURCE_TYPE,
    map_drive_item,
)
from opshub.core.document_extract import (
    GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE,
    GoogleWorkspaceSourceType,
    extract_workspace_export,
)

# ---------------------------------------------------------------------------
# Phase 13-specific fixture binaries.
#
# These are the files Phase 13 G5 (#279) committed under
# ``tests/fixtures/google_workspace/`` to back this test. They sat
# unreferenced (``rg`` returned zero hits across ``tests/``) until
# Cluster C wired them in here — the very gap issue #288 was opened to
# resolve. The path layout intentionally diverges from
# ``tests/fixtures/office/`` so a future churn that re-orders the
# Office fixtures cannot silently break the Phase 13 round-trip.
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "google_workspace"
_EXPORTED_DOC = _FIXTURES_DIR / "exported_doc.docx"
_EXPORTED_SHEET = _FIXTURES_DIR / "exported_sheet.xlsx"
_EXPORTED_SLIDES = _FIXTURES_DIR / "exported_slides.pptx"


# ---------------------------------------------------------------------------
# Drive API mediatypes the Drive API connector passes to
# ``files.export(fileId, mimeType=...)``. Mirrors
# :data:`opshub.connectors.google_workspace.connector._EXPORT_MEDIATYPE_BY_SOURCE_TYPE`
# (kept inline here so the test self-documents the round-trip's
# parameter shape end-to-end; a future plan-side refactor that
# adjusts the table would surface as both a connector and a test
# update, not a hidden diff).
# ---------------------------------------------------------------------------

_EXPORT_MEDIATYPE_BY_SOURCE_TYPE: dict[GoogleWorkspaceSourceType, str] = {
    "google_doc": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "google_slides": ("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "google_sheets": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


# ---------------------------------------------------------------------------
# Stubs.
# ---------------------------------------------------------------------------


class _StubAuth:
    """Minimal stand-in for :class:`GoogleWorkspaceAuth` used by the round-trip.

    :class:`DriveClient` only calls :meth:`get_access_token` on the
    auth helper, so a tiny stub is enough — the OAuth round-trip is
    covered by :mod:`tests.unit.connectors.google_auth.test_auth`
    (Phase 14 G2 moved the helper + its tests to
    :mod:`opshub.connectors.google_auth`).
    """

    def get_access_token(self) -> str:
        return "fake-access-token"


def _build_drive_client(handler: Any) -> DriveClient:
    """Build a :class:`DriveClient` whose underlying ``httpx`` uses ``handler``.

    Same pattern :func:`tests.unit.connectors.google_workspace.test_client._client_with_handler`
    uses — duplicated here so this integration test stays
    self-contained (no cross-test fixture imports). Closes the live
    client immediately after construction so the mock-bound replacement
    is the only socket the test ever touches.
    """
    transport = httpx.MockTransport(handler)
    client = DriveClient(cast(GoogleWorkspaceAuth, _StubAuth()))
    # ``DriveClient._client`` is documented private (the cast happens
    # in ``__init__`` for typing reasons). pyright (strict) would flag
    # the access; ``# pyright: ignore`` is the mypy-clean equivalent of
    # the same private-access escape hatch the sibling
    # :mod:`tests.unit.connectors.google_workspace.test_client` uses
    # (which still uses the older ``# type: ignore[reportPrivateUsage]``
    # form — that form trips mypy strict's ``unused-ignore`` rule).
    client._client.close()  # pyright: ignore[reportPrivateUsage]
    client._client = httpx.Client(transport=transport, timeout=5.0)  # pyright: ignore[reportPrivateUsage]
    return client


def _drive_file_factory(
    *,
    file_id: str,
    name: str,
    mime_type: str,
) -> Any:
    """Build the :class:`RawDriveItem`-equivalent payload the mapper consumes.

    We materialise the same shape ``client._normalise_change`` would
    produce, but skip the changes.list mock for the second half of the
    round-trip — the mapper boundary is the contract this integration
    test pins, not the changes.list pagination (which has dedicated
    coverage in :mod:`tests.unit.connectors.google_workspace.test_client`).
    """
    from opshub.connectors.google_workspace.client import RawDriveItem

    return RawDriveItem(
        file_id=file_id,
        removed=False,
        trashed=False,
        name=name,
        mime_type=mime_type,
        modified_time_iso="2026-05-31T12:00:00Z",
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        owner_email="alice@example.com",
        owner_display_name="Alice",
        is_shared_with_me=False,
        shared=False,
        last_modifying_user_email="",
        last_modifying_user_display_name="",
        drive_id="",
        raw={},
    )


# ---------------------------------------------------------------------------
# Round-trip cases — one per Phase 13 source_type.
#
# Each case carries:
#   * the fixture path (drives the ``rg`` hit Cluster C C#14 mandates)
#   * the Drive native mimeType (Google's source ``mime_type``)
#   * the canonical source_type (the discriminator stamped on the event)
#   * the export target mediatype (what the connector tells Drive to
#     emit so markitdown can pick the right converter via suffix)
# ---------------------------------------------------------------------------


_CASES: tuple[tuple[Path, str, GoogleWorkspaceSourceType, str], ...] = (
    (
        _EXPORTED_DOC,
        "application/vnd.google-apps.document",
        GOOGLE_DOC_SOURCE_TYPE,
        _EXPORT_MEDIATYPE_BY_SOURCE_TYPE["google_doc"],
    ),
    (
        _EXPORTED_SHEET,
        "application/vnd.google-apps.spreadsheet",
        GOOGLE_SHEETS_SOURCE_TYPE,
        _EXPORT_MEDIATYPE_BY_SOURCE_TYPE["google_sheets"],
    ),
    (
        _EXPORTED_SLIDES,
        "application/vnd.google-apps.presentation",
        GOOGLE_SLIDES_SOURCE_TYPE,
        _EXPORT_MEDIATYPE_BY_SOURCE_TYPE["google_slides"],
    ),
)


@pytest.mark.parametrize(
    ("fixture_path", "source_mime", "source_type", "export_mediatype"),
    _CASES,
    ids=("google_doc", "google_sheets", "google_slides"),
)
def test_workspace_export_roundtrip_persists_body_with_provenance(
    fixture_path: Path,
    source_mime: str,
    source_type: GoogleWorkspaceSourceType,
    export_mediatype: str,
) -> None:
    """End-to-end round-trip: fixture → Drive mock → markitdown → SourceObserved body.

    Cluster C C#14 (must) — fixture 死蔵解決. Plan §G5 DoD pin.

    Sequence:

    1. Read the Phase 13-specific fixture binary
       (``tests/fixtures/google_workspace/exported_*.{docx,xlsx,pptx}``)
       so the ``rg`` hit Cluster C C#14 mandates lands on the
       parametrised fixture references.
    2. Stand up a :class:`DriveClient` whose ``httpx`` transport is a
       :class:`httpx.MockTransport` that returns the fixture bytes for
       ``files.export`` calls. The mock asserts on the URL shape and
       the ``mimeType`` query parameter so a future drift in the
       export-mediatype table surfaces here.
    3. Invoke :meth:`DriveClient.export_file` to retrieve the bytes
       through the production code path (no shortcut around
       :meth:`DriveClient._request_bytes`).
    4. Hand the bytes to :func:`extract_workspace_export` for the
       markitdown round-trip — the body and ``source_type`` come back
       on the :class:`ExtractResult`.
    5. Build a :class:`RawDriveItem` carrying the Drive native
       ``source_mime`` and route it through :func:`map_drive_item`
       with the extracted body. The resulting :class:`SourceObserved`
       must:
       * carry the canonical Phase 13 ``source_type`` discriminator
         (``google_doc`` / ``google_slides`` / ``google_sheets``),
       * stamp the SaaS-family provenance (``external`` / ``untrusted``)
         so an LLM treats the body as reference material per
         ADR-0015 §決定 (f),
       * carry the extracted body verbatim (the mapper does not
         re-truncate — ADR-0025 §決定 (b-2) gives the extractor sole
         responsibility for truncation marks).
    """
    # ---- 1. fixture sanity ---------------------------------------------
    # The fixtures must actually exist on disk; without them the round-trip
    # is moot. This guard surfaces a missing-fixture failure mode as a
    # clear assertion rather than an opaque ``FileNotFoundError`` deep
    # inside markitdown.
    assert fixture_path.exists(), (
        f"Phase 13 fixture {fixture_path.name} missing — Cluster C C#14"
        f" requires the binaries committed by Phase 13 G5 (#279); cf."
        f" tests/fixtures/google_workspace/"
    )
    fixture_bytes = fixture_path.read_bytes()
    assert fixture_bytes, f"Phase 13 fixture {fixture_path.name} is empty"

    # The mimeType → source_type lookup the connector uses must agree
    # with the case table — this is the small contract the Cluster C
    # plan calls "the export side of the responsibility split".
    assert GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE[source_mime] == source_type

    # ---- 2. Drive API mock --------------------------------------------
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url).split("?", 1)[0]
        captured["params"] = dict(request.url.params)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=fixture_bytes)

    client = _build_drive_client(handler)

    # ---- 3. files.export via the production code path ----------------
    try:
        export_bytes = client.export_file(
            file_id="FAKE-FILE-ID",
            mime_type=export_mediatype,
        )
    finally:
        client.close()

    # The mock saw the right URL + mimeType — pins the connector's
    # outbound shape end-to-end.
    assert export_bytes == fixture_bytes
    assert captured["url"] == f"{DRIVE_API_BASE}/files/FAKE-FILE-ID/export"
    assert captured["params"]["mimeType"] == export_mediatype
    assert captured["auth"] == "Bearer fake-access-token"

    # ---- 4. markitdown round-trip --------------------------------------
    result = extract_workspace_export(export_bytes, source_type)

    # The markitdown path actually ran — body is a non-empty string
    # (not the empty-bytes short-circuit, not the size-cap skip).
    assert result.body is not None, (
        f"extract_workspace_export returned body=None for {fixture_path.name};"
        f" skip_reason={result.skip_reason!r}"
    )
    assert result.body != ""
    assert result.skip_reason is None
    assert result.truncated is False
    # The discriminator survives the round-trip — important because a
    # regression that re-stamped from the tempfile suffix would
    # mis-route Sheets → google_doc on the projection.
    assert result.source_type == source_type

    # ---- 5. mapper + provenance ---------------------------------------
    raw = _drive_file_factory(
        file_id="FAKE-FILE-ID",
        name=f"Phase 13 — {source_type}",
        mime_type=source_mime,
    )
    event = map_drive_item(raw, body=result.body)

    # The event carries the Phase 13 discriminator (not the Office
    # equivalent the Drive export bytes look like on disk).
    assert event.source_type == source_type
    assert event.external_id == "FAKE-FILE-ID"
    assert event.title == f"Phase 13 — {source_type}"
    # The body is forwarded verbatim — the mapper does not re-truncate
    # or reformat what the extractor handed it (ADR-0025 §決定 (b-2)).
    assert event.body == result.body
    # Provenance stamps match the SaaS family invariant — ADR-0020 §(e)
    # tells the secretary / LLM prompts to treat the body as reference
    # material under the do-not-follow preamble (ADR-0015 §決定 (f)).
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"
    # Connector identity reaches the event so the projection can
    # answer "which connector produced this?" without joining back to
    # the source raw.
    assert event.connector_name == "google_workspace"


def test_fixture_directory_contains_three_phase13_binaries() -> None:
    """The Phase 13 fixture directory carries exactly the three committed binaries.

    Guards against a quiet fixture-trim (a future ``git clean`` /
    ``rm`` accidentally dropping a binary). Phase 13 G5 (#279)
    committed three files; we pin the set so any future change to it
    has to update this test too.
    """
    files = sorted(p.name for p in _FIXTURES_DIR.iterdir() if p.is_file())
    assert files == [
        "exported_doc.docx",
        "exported_sheet.xlsx",
        "exported_slides.pptx",
    ], (
        f"Phase 13 fixture directory contents drifted; got: {files}."
        " The three binaries are the Phase 13 plan §G5 DoD contract"
        " (issue #288 Cluster C C#14)."
    )
