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
    POWERPOINT_SOURCE_TYPE,
    SOURCE_TYPE_BY_EXTENSION,
    WORD_SOURCE_TYPE,
    ExtractResult,
    extract_document,
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
