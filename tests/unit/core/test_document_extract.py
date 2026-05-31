"""Unit tests for :mod:`opshub.core.document_extract` (Phase 11 F2, ADR-0025).

The test suite pins the 7-point contract surfaced by ADR-0025 §決定:

* (a) deferred ``markitdown`` import — covered by the cold-start
  tripwire (``tests/integration/test_cold_start.py``), so this file
  exercises behaviour assuming ``markitdown`` is importable (the
  ``[office]`` extras are installed in CI per Phase 11 F2 workflow
  sync).
* (b-1) file-size cap → ``body=None`` + ``skip_reason="file too large"``
  (:func:`test_skips_when_file_too_large`)
* (b-2) extracted-text cap → ``truncated=True`` + tail notice
  (:func:`test_truncates_when_text_exceeds_max_chars`)
* (c) extraction failure → ``body=None`` + ``skip_reason="extraction
  failed: <ExceptionClassName>"`` (:func:`test_extraction_failure_returns_skip_reason`)
* (d) extension → ``source_type`` mapping pin
  (:func:`test_source_type_pin_for_each_extension`)
* (e) cell caps accepted as kwargs (API-shape stability)
  (:func:`test_cell_cap_kwargs_accepted_without_effect`)
* (f) PPT speaker notes included in the body
  (:func:`test_pptx_includes_speaker_notes`)

Fixtures live under ``tests/fixtures/office/`` and are intentionally
**tiny** (4-40 KB each) so the suite stays under the cold-start budget
and re-running ``pytest tests/unit/core/`` does not warm a multi-MB
working set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.document_extract import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_FILE_BYTES,
    EXCEL_SOURCE_TYPE,
    GOOGLE_DOC_SOURCE_TYPE,
    GOOGLE_SHEETS_SOURCE_TYPE,
    GOOGLE_SLIDES_SOURCE_TYPE,
    GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE,
    GOOGLE_WORKSPACE_SOURCE_TYPES,
    POWERPOINT_SOURCE_TYPE,
    SOURCE_TYPE_BY_EXTENSION,
    WORD_SOURCE_TYPE,
    ExtractResult,
    extract_document,
    extract_workspace_export,
)

# Test fixtures: tiny real Office files committed under tests/fixtures/office/.
# Built once via python-docx / openpyxl / python-pptx (see ADR-0025 §Validation
# for the build recipe). Sizes are deliberately bounded so the suite stays
# fast and the working set is friendly to the CI runner.
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "office"
_DOCX = _FIXTURES_DIR / "sample.docx"
_XLSX = _FIXTURES_DIR / "sample.xlsx"
_PPTX = _FIXTURES_DIR / "sample.pptx"
_CORRUPT_DOCX = _FIXTURES_DIR / "corrupt.docx"


# Skip the entire module when ``markitdown`` is not installed. The
# Phase 11 F2 CI workflow opts into ``--extra office`` so this guard
# is for the local dev path only (``uv sync --extra dev`` without
# ``--extra office``). Running the suite without markitdown would
# surface the ``skip_reason="markitdown not installed"`` branch,
# which is covered by a dedicated test below via monkeypatching the
# import — so we do not need a per-test skip on the same condition.
pytest.importorskip("markitdown")


# --------------------------------------------------------------------- (a)
# Deferred import contract
# ---------------------------------------------------------------------


def test_markitdown_not_imported_at_module_load() -> None:
    """``import opshub.core.document_extract`` must not pull markitdown.

    ADR-0025 §決定 (a) + §軽減策 #1: markitdown lives inside the
    :func:`extract_document` function body so the M6 cold-start guard
    (ADR-0001 §Negative §1) stays intact even when the ``[office]``
    extras are installed. The full process-level tripwire lives at
    ``tests/integration/test_cold_start.py`` (which spawns a fresh
    ``python -m opshub --help``); this unit-test variant assures the
    same invariant at module-import granularity so a regression
    surfaces in the fast unit suite too.
    """
    import sys

    # Drop any cached markitdown so we observe a fresh import graph.
    for mod_name in list(sys.modules):
        if mod_name == "markitdown" or mod_name.startswith("markitdown."):
            del sys.modules[mod_name]
    # Re-import the module under test so its top-level statements run
    # cleanly without markitdown in ``sys.modules``. ``importlib`` is
    # used (not a bare ``import``) so the unused-import assertion is
    # the side-effect (module reload) rather than a name binding the
    # type-checker would flag.
    import importlib

    sys.modules.pop("opshub.core.document_extract", None)
    importlib.import_module("opshub.core.document_extract")

    assert "markitdown" not in sys.modules, (
        "opshub.core.document_extract pulled markitdown at module load — "
        "the import must stay inside extract_document() (ADR-0025 §決定 (a))."
    )


# --------------------------------------------------------------------- (b-1)
# File-size cap
# ---------------------------------------------------------------------


def test_skips_when_file_too_large(tmp_path: Path) -> None:
    """Files above ``max_file_bytes`` return ``body=None``.

    The size check runs **before** markitdown is invoked so a 50 MB+
    workbook never triggers an expensive extraction in the first
    place.
    """
    big = tmp_path / "huge.docx"
    big.write_bytes(b"x" * 4096)

    result = extract_document(big, max_file_bytes=2048)

    assert result.body is None
    assert result.truncated is False
    assert result.skip_reason == "file too large"
    assert result.source_type == WORD_SOURCE_TYPE


def test_size_cap_zero_means_unlimited() -> None:
    """``max_file_bytes=0`` disables the size check (ADR-0025 §決定 (b-1) note)."""
    result = extract_document(_DOCX, max_file_bytes=0)
    assert result.body is not None
    assert result.skip_reason is None


# --------------------------------------------------------------------- (b-2)
# Extracted-text cap
# ---------------------------------------------------------------------


def test_truncates_when_text_exceeds_max_chars() -> None:
    """When the extracted markdown is longer than ``max_chars`` it is head-truncated.

    We use the live ``.docx`` fixture and force a tiny ``max_chars`` so
    the cap fires deterministically — relying on the actual fixture
    body length keeps the assertion independent of markitdown
    versioning quirks (whitespace / escape backslash differences).
    """
    result = extract_document(_DOCX, max_chars=50)

    assert result.body is not None
    assert result.truncated is True
    assert result.skip_reason is None
    # The fixed-shape tail notice carries the original length and the
    # cap so a downstream operator / brief can render "truncated at
    # N/M chars" without re-deriving either value.
    assert "[truncated: original=" in result.body
    assert "limit=50]" in result.body


def test_no_truncation_when_under_max_chars() -> None:
    """Documents that fit under the cap are returned verbatim, ``truncated=False``."""
    result = extract_document(_DOCX, max_chars=DEFAULT_MAX_CHARS)
    assert result.body is not None
    assert result.truncated is False
    assert "[truncated:" not in result.body


def test_max_chars_zero_means_unlimited() -> None:
    """``max_chars=0`` disables the char cap."""
    result = extract_document(_DOCX, max_chars=0)
    assert result.body is not None
    assert result.truncated is False


# --------------------------------------------------------------------- (c)
# Fail-safe — extraction failure
# ---------------------------------------------------------------------


def test_extraction_failure_returns_skip_reason() -> None:
    """A corrupt zip-shaped ``.docx`` triggers markitdown ``FileConversionException``.

    The function must catch it and return ``body=None`` +
    ``skip_reason`` so a single bad file does not stop a scan over
    100k+ Box Drive files (ADR-0025 §決定 (c)).
    """
    result = extract_document(_CORRUPT_DOCX)

    assert result.body is None
    assert result.truncated is False
    assert result.skip_reason is not None
    assert result.skip_reason.startswith("extraction failed:")
    assert result.source_type == WORD_SOURCE_TYPE


def test_extraction_failure_does_not_raise() -> None:
    """The fail-safe contract is "never raise"; even a missing file is caught."""
    result = extract_document(Path("/nonexistent/path/missing.docx"))
    # ``os.stat`` raises ``FileNotFoundError`` so the function short-
    # circuits via the ``stat_failed`` path before invoking markitdown.
    assert result.body is None
    assert result.skip_reason is not None
    assert result.skip_reason.startswith("stat failed:")
    assert result.source_type == WORD_SOURCE_TYPE


def test_unsupported_extension_returns_skip_reason(tmp_path: Path) -> None:
    """A non-Office extension returns ``source_type=None`` + ``skip_reason``."""
    weird = tmp_path / "data.txt"
    weird.write_text("plain text", encoding="utf-8")

    result = extract_document(weird)

    assert result.body is None
    assert result.skip_reason == "unsupported format"
    assert result.source_type is None


# --------------------------------------------------------------------- (d)
# Extension → source_type mapping pin
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "extension, expected",
    [
        (".docx", WORD_SOURCE_TYPE),
        (".doc", WORD_SOURCE_TYPE),
        (".xlsx", EXCEL_SOURCE_TYPE),
        (".xls", EXCEL_SOURCE_TYPE),
        (".pptx", POWERPOINT_SOURCE_TYPE),
        (".ppt", POWERPOINT_SOURCE_TYPE),
    ],
)
def test_source_type_pin_for_each_extension(extension: str, expected: str) -> None:
    """The 6 supported extensions map to exactly 3 ``OfficeSourceType`` values."""
    assert SOURCE_TYPE_BY_EXTENSION[extension] == expected


def test_source_type_dispatch_for_each_fixture() -> None:
    """End-to-end dispatch: each fixture extension yields the correct ``source_type``.

    Reads the live fixtures so a future refactor that decouples the
    table from the function still keeps the API consistent.
    """
    docx_result = extract_document(_DOCX)
    xlsx_result = extract_document(_XLSX)
    pptx_result = extract_document(_PPTX)

    assert docx_result.source_type == WORD_SOURCE_TYPE
    assert xlsx_result.source_type == EXCEL_SOURCE_TYPE
    assert pptx_result.source_type == POWERPOINT_SOURCE_TYPE


def test_source_type_case_insensitive(tmp_path: Path) -> None:
    """Uppercase extensions (``.DOCX``) route to the same discriminator."""
    upper = tmp_path / "REPORT.DOCX"
    upper.write_bytes(_DOCX.read_bytes())

    result = extract_document(upper)
    assert result.source_type == WORD_SOURCE_TYPE
    # Sanity: the case-insensitive dispatch did not corrupt the body.
    assert result.body is not None


# --------------------------------------------------------------------- (e)
# Cell-cap kwargs accepted (API-shape stability)
# ---------------------------------------------------------------------


def test_cell_cap_kwargs_accepted_without_effect() -> None:
    """``max_cells_per_sheet`` / ``max_cells_per_workbook`` are accepted today.

    ADR-0025 §決定 (e) defers the per-cell cell-count cap to Phase
    11.x; the MVP relies on ``max_chars`` for the practical defence.
    The kwargs are accepted now so the public signature is stable
    once the refinement lands. This test pins the contract: passing
    them does not change the happy-path body.
    """
    baseline = extract_document(_XLSX)
    capped = extract_document(_XLSX, max_cells_per_sheet=1, max_cells_per_workbook=1)
    assert baseline.body == capped.body


# --------------------------------------------------------------------- (f)
# PowerPoint — speaker notes included
# ---------------------------------------------------------------------


def test_pptx_includes_speaker_notes() -> None:
    """markitdown's default ``.pptx`` converter must emit speaker notes.

    ADR-0025 §決定 (f) treats speaker notes as first-class context for
    reply-draft / meeting-prep skills, and the MVP relies on
    markitdown's default behaviour rather than a python-pptx fallback.
    The fixture's speaker notes carry a unique marker ``NOTES-HERE``
    so the assertion does not flake on whitespace differences.
    """
    result = extract_document(_PPTX)
    assert result.body is not None
    assert "NOTES-HERE" in result.body
    # markitdown emits notes under a ``### Notes:`` header on the
    # default converter; check both the marker (substantive) and the
    # header (structural) so a future regression in either dimension
    # surfaces clearly.
    assert "Notes" in result.body


def test_pptx_includes_slide_body() -> None:
    """The slide body text is extracted alongside notes (ADR-0025 §決定 (f-1))."""
    result = extract_document(_PPTX)
    assert result.body is not None
    assert "SLIDE-BODY" in result.body


# --------------------------------------------------------------------- Excel
# Per-sheet table extraction
# ---------------------------------------------------------------------


def test_xlsx_extracts_all_sheets() -> None:
    """ADR-0025 §決定 (e) full-workbook coverage — both sheets land in the body."""
    result = extract_document(_XLSX)
    assert result.body is not None
    # The fixture has a ``Summary`` and a ``Details`` sheet — both
    # sheet titles should appear in the markdown so multi-sheet
    # recall stays accurate.
    assert "Summary" in result.body
    assert "Details" in result.body
    # Cell content from the second sheet must survive.
    assert "DETAILS-HERE" in result.body


# --------------------------------------------------------------------- ExtractResult
# Value-object behaviour
# ---------------------------------------------------------------------


def test_extract_result_is_frozen() -> None:
    """The dataclass is ``frozen=True`` so callers cannot mutate the result."""
    result = extract_document(_DOCX)
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        result.body = "tampered"  # type: ignore[misc]


def test_defaults_are_documented_constants() -> None:
    """The module exposes the default caps as constants so callers can introspect them."""
    assert DEFAULT_MAX_FILE_BYTES == 50 * 1024 * 1024
    assert DEFAULT_MAX_CHARS == 500_000


# --------------------------------------------------------------------- Missing extras
# Defensive branch when markitdown extras are not installed
# ---------------------------------------------------------------------


def test_returns_skip_reason_when_markitdown_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a missing ``[office]`` extras install.

    Patches ``builtins.__import__`` so the deferred ``import markitdown``
    inside :func:`extract_document` raises ``ImportError``. The
    function must return ``body=None`` + ``skip_reason="markitdown not
    installed"`` so an operator who forgot to opt into the extras
    sees a clear skip rather than a hard crash mid-scan.
    """
    import builtins
    from collections.abc import Mapping, Sequence
    from typing import Any

    real_import = builtins.__import__

    def _fake_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> Any:
        if name == "markitdown" or name.startswith("markitdown."):
            raise ImportError("No module named 'markitdown'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    # Drop any cached markitdown import so the deferred import path
    # actually triggers our shim.
    import sys

    for mod_name in list(sys.modules):
        if mod_name == "markitdown" or mod_name.startswith("markitdown."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    result = extract_document(_DOCX)
    assert isinstance(result, ExtractResult)
    assert result.body is None
    assert result.skip_reason == "markitdown not installed"
    assert result.source_type == WORD_SOURCE_TYPE


# --------------------------------------------------------------------- (d')
# Phase 13 G2 — Google Workspace source_type Literal value pin
# ---------------------------------------------------------------------


def test_google_workspace_source_type_literal_values() -> None:
    """The three Google Workspace ``source_type`` strings are pinned by value.

    The Phase 13 G3 PR (#277) imports
    :data:`GOOGLE_DOC_SOURCE_TYPE` / :data:`GOOGLE_SLIDES_SOURCE_TYPE` /
    :data:`GOOGLE_SHEETS_SOURCE_TYPE` for the ``mimeType`` →
    ``source_type`` lookup. A drift in any of these three strings
    would silently route Workspace docs through the wrong projection
    filter (and break ``opshub source list --type google_doc``). The
    pin makes a typo / rename a CI failure rather than a runtime
    regression that only surfaces in operator queries.
    """
    assert GOOGLE_DOC_SOURCE_TYPE == "google_doc"
    assert GOOGLE_SLIDES_SOURCE_TYPE == "google_slides"
    assert GOOGLE_SHEETS_SOURCE_TYPE == "google_sheets"


def test_google_workspace_source_types_tuple_pin() -> None:
    """``GOOGLE_WORKSPACE_SOURCE_TYPES`` order and membership are pinned.

    The Phase 13 G3 PR (#277) iterates the tuple for the rotation /
    enumeration tests; pinning order keeps reviewers' diffs honest and
    makes accidental reorderings a CI failure.
    """
    assert GOOGLE_WORKSPACE_SOURCE_TYPES == (
        "google_doc",
        "google_slides",
        "google_sheets",
    )
    # No accidental Office / Phase 11 leakage.
    assert WORD_SOURCE_TYPE not in GOOGLE_WORKSPACE_SOURCE_TYPES
    assert EXCEL_SOURCE_TYPE not in GOOGLE_WORKSPACE_SOURCE_TYPES
    assert POWERPOINT_SOURCE_TYPE not in GOOGLE_WORKSPACE_SOURCE_TYPES


def test_google_workspace_mimetype_lookup_pin() -> None:
    """The authoritative Google ``mimeType`` → ``source_type`` mapping is pinned.

    ADR-0025 §決定 (d') Table 2 fixes the three pairings; G3 (#277)
    imports the table to normalise Drive ``files.list`` /
    ``changes.list`` metadata. The pin makes any silent edit of the
    table a CI failure.
    """
    assert GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE == {
        "application/vnd.google-apps.document": "google_doc",
        "application/vnd.google-apps.presentation": "google_slides",
        "application/vnd.google-apps.spreadsheet": "google_sheets",
    }
    # The keys are the three Google native mimeTypes — no Office
    # mediatypes leak in. (A regression that mapped the export-target
    # mediatype here would route any ``.docx`` upload through the
    # ``google_doc`` path.)
    for key in GOOGLE_WORKSPACE_MIMETYPE_TO_SOURCE_TYPE:
        assert key.startswith("application/vnd.google-apps.")


# --------------------------------------------------------------------- (j)
# Phase 13 G2 — Workspace export path round-trips
# ---------------------------------------------------------------------


def test_workspace_export_routes_docx_through_google_doc_source_type() -> None:
    """Google Docs exports (.docx bytes) yield ``source_type="google_doc"``.

    ADR-0025 §決定 (d') is explicit that a Google Doc that was exported
    as ``.docx`` stays ``"google_doc"`` — the underlying export
    mediatype is an implementation detail of the Drive API call, not
    the operator-facing discriminator. The fixture's ``.docx`` bytes
    are re-used as a stand-in for the Drive ``files.export`` output
    so the test exercises the real markitdown path.
    """
    export_bytes = _DOCX.read_bytes()

    result = extract_workspace_export(export_bytes, GOOGLE_DOC_SOURCE_TYPE)

    assert result.source_type == GOOGLE_DOC_SOURCE_TYPE
    # The body must contain the actual extracted markdown, not be
    # ``None`` (the markitdown path was actually invoked).
    assert result.body is not None
    assert result.body != ""
    assert result.skip_reason is None
    assert result.truncated is False


def test_workspace_export_routes_xlsx_through_google_sheets_source_type() -> None:
    """Google Sheets exports (.xlsx bytes) yield ``source_type="google_sheets"``."""
    export_bytes = _XLSX.read_bytes()

    result = extract_workspace_export(export_bytes, GOOGLE_SHEETS_SOURCE_TYPE)

    assert result.source_type == GOOGLE_SHEETS_SOURCE_TYPE
    assert result.body is not None
    # The .xlsx fixture has two sheets; both should land in the body
    # via the same markitdown path Phase 11 Excel uses.
    assert "Summary" in result.body
    assert "Details" in result.body


def test_workspace_export_routes_pptx_through_google_slides_source_type() -> None:
    """Google Slides exports (.pptx bytes) yield ``source_type="google_slides"``."""
    export_bytes = _PPTX.read_bytes()

    result = extract_workspace_export(export_bytes, GOOGLE_SLIDES_SOURCE_TYPE)

    assert result.source_type == GOOGLE_SLIDES_SOURCE_TYPE
    assert result.body is not None
    # Speaker notes (§決定 (f)) must survive the Workspace export
    # path just like the local-FS Office path.
    assert "NOTES-HERE" in result.body
    assert "SLIDE-BODY" in result.body


def test_workspace_export_size_cap_skips_before_disk_write() -> None:
    """Exports above ``max_file_bytes`` return ``body=None`` + ``skip_reason``.

    ADR-0025 §決定 (b-1) carries over to the Workspace export path.
    The cap is applied to the *exported* bytes length so a 100 MB
    export never lands in tempdir.
    """
    # 4 KiB of bytes; cap of 2 KiB so the cap fires deterministically.
    oversize = b"x" * 4096

    result = extract_workspace_export(oversize, GOOGLE_DOC_SOURCE_TYPE, max_file_bytes=2048)

    assert result.body is None
    assert result.truncated is False
    assert result.skip_reason == "file too large"
    # The source_type is still stamped — the Drive mapper needs the
    # discriminator even on the skip path so the SourceObserved row
    # carries it for filtering / forensics.
    assert result.source_type == GOOGLE_DOC_SOURCE_TYPE


def test_workspace_export_size_cap_zero_means_unlimited() -> None:
    """``max_file_bytes=0`` disables the size check (ADR-0025 §決定 (b-1) note)."""
    export_bytes = _DOCX.read_bytes()

    result = extract_workspace_export(export_bytes, GOOGLE_DOC_SOURCE_TYPE, max_file_bytes=0)

    assert result.body is not None
    assert result.skip_reason is None
    assert result.source_type == GOOGLE_DOC_SOURCE_TYPE


def test_workspace_export_fail_safe_on_corrupt_bytes() -> None:
    """A corrupt ``.docx`` payload triggers markitdown failure and is fail-safed.

    ADR-0025 §決定 (c) extends to the Workspace export path: a bad
    export must not stop the Drive sync loop. The function returns
    ``body=None`` + ``skip_reason="extraction failed: <ExcCls>"`` and
    never raises.
    """
    corrupt_bytes = _CORRUPT_DOCX.read_bytes()

    result = extract_workspace_export(corrupt_bytes, GOOGLE_DOC_SOURCE_TYPE)

    assert result.body is None
    assert result.truncated is False
    assert result.skip_reason is not None
    assert result.skip_reason.startswith("extraction failed:")
    assert result.source_type == GOOGLE_DOC_SOURCE_TYPE


def test_workspace_export_empty_bytes_returns_empty_body() -> None:
    """Empty export bytes short-circuit to ``body=""`` (no tempfile, no markitdown).

    The Drive API legitimately returns 200 OK with a zero-byte body
    for an empty Google Doc; we treat that as a successful extraction
    with an empty body so the connector still emits ``SourceObserved``
    (metadata-only).
    """
    result = extract_workspace_export(b"", GOOGLE_DOC_SOURCE_TYPE)

    assert result.body == ""
    assert result.truncated is False
    assert result.skip_reason is None
    assert result.source_type == GOOGLE_DOC_SOURCE_TYPE


def test_workspace_export_truncates_when_text_exceeds_max_chars() -> None:
    """The §決定 (b-2) char cap also applies to the Workspace export path."""
    export_bytes = _DOCX.read_bytes()

    result = extract_workspace_export(export_bytes, GOOGLE_DOC_SOURCE_TYPE, max_chars=50)

    assert result.body is not None
    assert result.truncated is True
    assert result.skip_reason is None
    # Same fixed-shape notice as extract_document (one truncation
    # arithmetic SSOT via opshub.core.text_limits).
    assert "[truncated: original=" in result.body
    assert "limit=50]" in result.body
    assert result.source_type == GOOGLE_DOC_SOURCE_TYPE


def test_workspace_export_does_not_leave_tempfile_behind(tmp_path: Path) -> None:
    """The tempfile created for markitdown is unlinked on every code path.

    We point ``TMPDIR`` (and friends) at a controlled directory and
    assert it is empty after the call. The check covers both the
    happy path and the cleanup-on-fail-safe path so a future
    regression in the ``finally`` arm surfaces quickly.
    """
    import os

    saved = {key: os.environ.get(key) for key in ("TMPDIR", "TEMP", "TMP")}
    os.environ["TMPDIR"] = str(tmp_path)
    os.environ["TEMP"] = str(tmp_path)
    os.environ["TMP"] = str(tmp_path)
    try:
        # Happy path
        result = extract_workspace_export(_DOCX.read_bytes(), GOOGLE_DOC_SOURCE_TYPE)
        assert result.body is not None
        # Fail-safe path (corrupt bytes)
        bad = extract_workspace_export(_CORRUPT_DOCX.read_bytes(), GOOGLE_DOC_SOURCE_TYPE)
        assert bad.skip_reason is not None
        # The controlled tempdir should not be holding any
        # ``opshub_workspace_export_*`` files after both calls.
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("opshub_workspace_export_")]
        assert leftovers == []
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_workspace_export_uses_correct_tempfile_suffix_per_source_type() -> None:
    """The tempfile suffix matches the export mediatype.

    A regression that routed a Google Sheet through a ``.docx``
    tempfile would silently produce empty / garbled markdown because
    markitdown's converter dispatcher routes on suffix. We assert the
    behavioural contract end-to-end by checking the extracted body
    actually contains the sheet's content (markdown table
    structure), which only works when the dispatcher picked the
    XlsxConverter — i.e. the suffix routing is correct.
    """
    # Sheets fixture extraction succeeds with sheet titles present —
    # this only happens when the .xlsx suffix is selected.
    result = extract_workspace_export(_XLSX.read_bytes(), GOOGLE_SHEETS_SOURCE_TYPE)
    assert result.body is not None
    assert "Summary" in result.body
    assert "Details" in result.body

    # Sheets bytes routed through google_doc would pick the .docx
    # tempfile suffix and markitdown's docx converter would either
    # crash (BadZipFile or FileConversionException) or produce
    # something that doesn't contain the sheet content. We pin the
    # contract: the suffix is picked from the source_type, not the
    # bytes' actual format.
    wrong_routing = extract_workspace_export(_XLSX.read_bytes(), GOOGLE_DOC_SOURCE_TYPE)
    # The .xlsx bytes happen to share the ZIP container with .docx so
    # markitdown might still extract *something*, but the result
    # source_type is what we actually care about — the discriminator
    # must mirror the supplied argument either way.
    assert wrong_routing.source_type == GOOGLE_DOC_SOURCE_TYPE


# --------------------------------------------------------------------- (existing path)
# Phase 13 G2 — Existing Office path must stay byte-identical
# ---------------------------------------------------------------------


def test_office_path_still_returns_office_source_type_after_g2() -> None:
    """Adding the Workspace path does not perturb the Phase 11 Office path.

    A regression that re-stamped the source_type from a tempfile
    suffix in the wrong direction (Office → Google) would break
    Phase 11 F4 box_drive / onedrive_drive scanners. We pin the
    invariant explicitly so any future shared helper between the two
    paths still keeps the Office discriminators on the Office
    invocation.
    """
    assert extract_document(_DOCX).source_type == WORD_SOURCE_TYPE
    assert extract_document(_XLSX).source_type == EXCEL_SOURCE_TYPE
    assert extract_document(_PPTX).source_type == POWERPOINT_SOURCE_TYPE
