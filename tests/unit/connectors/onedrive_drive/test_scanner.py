"""Unit tests for :class:`opshub.connectors.onedrive_drive.scanner.OneDriveDriveScanner`.

The OneDrive scanner subclasses :class:`BoxDriveScanner` so the
heavy walk-logic coverage already lives in
``tests/unit/connectors/box_drive/test_scanner.py``. These tests
pin the subclass-specific guarantees:

* Inherited walk semantics still work end-to-end (smoke test).
* ConfigError messages name the OneDrive setup doc rather than the
  Box Drive one (operator-facing diagnostics).
* The class-level log-prefix override propagates through the
  scan loop (structured-log namespacing).
* The ADR-0019 §不変条件 (b) no-``open()`` invariant carries
  over verbatim on the default-off path.
* The ADR-0019 §(b') content_extraction opt-in carries over
  verbatim (Office extraction enabled / fail-safe behaviour).

The full walk-logic suite (symlink loops, max_depth / max_files
caps, permission errors, exclude_globs, fingerprint diff, etc.)
is covered by the box_drive tests and does not need re-running
here — the subclass inherits the exact same code path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opshub.connectors.onedrive_drive import OneDriveDriveScanner
from opshub.core.errors import ConfigError

# ---------------------------------------------------------------------------
# Inherited walk semantics — smoke test
# ---------------------------------------------------------------------------


def test_onedrive_scanner_yields_all_files_on_first_scan(tmp_path: Path) -> None:
    """Smoke test that the inherited walk loop reaches the OneDrive scanner.

    The walk-logic / fingerprint / removal contracts are pinned by
    the box_drive suite. This test exists so a regression that breaks
    the subclass MRO (e.g. accidentally shadowing ``scan()`` with a
    no-op) surfaces immediately on the OneDrive package's own test
    target.
    """
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("b")

    scanner = OneDriveDriveScanner(root_path=tmp_path)
    results = list(scanner.scan(prior_fingerprints={}))

    rel_paths = sorted(f.rel_path for f in results)
    assert rel_paths == ["a.txt", "sub/b.md"]


def test_onedrive_scanner_exposes_root_path(tmp_path: Path) -> None:
    """``root_path`` is exposed read-only — inherited from box_drive."""
    scanner = OneDriveDriveScanner(root_path=tmp_path)
    assert scanner.root_path == tmp_path


def test_onedrive_scanner_skips_unchanged_files(tmp_path: Path) -> None:
    """Fingerprint diff carries over from the base scanner."""
    (tmp_path / "stable.txt").write_text("stable")
    scanner = OneDriveDriveScanner(root_path=tmp_path)

    [first] = list(scanner.scan(prior_fingerprints={}))
    second = list(scanner.scan(prior_fingerprints={first.rel_path: first.fingerprint}))

    assert second == []


# ---------------------------------------------------------------------------
# Subclass-specific overrides — ConfigError messages
# ---------------------------------------------------------------------------


def test_onedrive_scanner_config_error_names_onedrive_setup_doc(tmp_path: Path) -> None:
    """A missing ``root_path`` surfaces ``onedrive-drive-setup.md`` in the message.

    ADR-0019 §(j-2) requires per-connector setup docs so operators
    landing on the error know which client to install. We pin the
    pointer here so a refactor that accidentally inherits the
    box_drive message (the most likely regression after the
    subclass-with-class-vars factoring) fails fast.
    """
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ConfigError, match=r"onedrive-drive-setup\.md"):
        OneDriveDriveScanner(root_path=missing)


def test_onedrive_scanner_config_error_names_onedrive_client(tmp_path: Path) -> None:
    """The 'not a directory' branch identifies the client as 'OneDrive'.

    Pinned for operator clarity — the message must read 'OneDrive
    root_path is not a directory' rather than the inherited 'Box Drive
    root_path is not a directory'.
    """
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("data")
    with pytest.raises(ConfigError, match="OneDrive root_path is not a directory"):
        OneDriveDriveScanner(root_path=not_a_dir)


# ---------------------------------------------------------------------------
# Subclass-specific overrides — structured-log key prefix
# ---------------------------------------------------------------------------


def test_onedrive_scanner_uses_onedrive_log_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The class-level ``_log_prefix`` override propagates into the scan loop.

    We trigger the ``scan_max_depth_reached`` branch (the cheapest
    way to surface a warning) and capture the structured-log event
    name. The OneDrive scanner must emit
    ``onedrive_drive.scan_max_depth_reached`` rather than the inherited
    ``box_drive.scan_max_depth_reached``; otherwise a unified-log
    query like ``filter event_type startswith 'onedrive_drive'`` would
    miss OneDrive events.
    """
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("deep")

    captured: list[str] = []

    def fake_warning(*args: Any, **_kwargs: Any) -> None:
        # structlog dispatches positional ``event`` as the first arg.
        if args:
            captured.append(str(args[0]))

    class _StubLogger:
        warning = staticmethod(fake_warning)

    # Replace the module-level logger used by the (shared) base scanner
    # walk loop. ``monkeypatch.setattr`` with a string path avoids
    # reading the private attribute directly from Python code
    # (silencing pyright's reportPrivateUsage) while still pinning
    # the same runtime behaviour: any warning emitted during scan is
    # captured.
    monkeypatch.setattr("opshub.connectors.box_drive.scanner._log", _StubLogger())

    scanner = OneDriveDriveScanner(root_path=tmp_path, max_depth=0)
    list(scanner.scan(prior_fingerprints={}))

    onedrive_events = [e for e in captured if e.startswith("onedrive_drive.")]
    box_drive_events = [e for e in captured if e.startswith("box_drive.")]
    assert onedrive_events, f"expected onedrive_drive.* events, got {captured!r}"
    assert not box_drive_events, (
        f"unexpected box_drive.* events from OneDriveDriveScanner: {box_drive_events!r}"
    )


# ---------------------------------------------------------------------------
# Inherited ADR-0019 §不変条件 (b) no-open() invariant — default-off path
# ---------------------------------------------------------------------------


def test_onedrive_scanner_never_opens_files_on_default_off_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019 §不変条件 (b) carries over: no ``open()`` on the default path.

    The base scanner's invariant is the entire reason the
    local-FS connector family is acceptable to ship; we pin it on
    the OneDrive subclass too so a future override that adds a
    walk-time ``open()`` for OneDrive-specific reasons (e.g. CldAPI
    metadata sniffing) fails the test rather than silently violating
    the contract.
    """
    (tmp_path / "a.txt").write_text("contents-a")
    (tmp_path / "report.docx").write_text("fake-docx")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.md").write_text("contents-b")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            f"OneDriveDriveScanner called a file-content primitive "
            f"(ADR-0019 §不変条件 (b) violation): args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    scanner = OneDriveDriveScanner(root_path=tmp_path)
    results = list(scanner.scan(prior_fingerprints={}))

    rel_paths = sorted(f.rel_path for f in results)
    assert rel_paths == ["a.txt", "nested/b.md", "report.docx"]


# ---------------------------------------------------------------------------
# Inherited ADR-0019 §(b') content_extraction opt-in
# ---------------------------------------------------------------------------


_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "office"
_DOCX_FIXTURE = _FIXTURES_DIR / "sample.docx"
_XLSX_FIXTURE = _FIXTURES_DIR / "sample.xlsx"
_PPTX_FIXTURE = _FIXTURES_DIR / "sample.pptx"
_CORRUPT_DOCX_FIXTURE = _FIXTURES_DIR / "corrupt.docx"


def _copy_fixtures_into(target_dir: Path) -> None:
    """Copy the Phase 11 F2 Office fixtures into ``target_dir``."""
    import shutil

    for fixture in (_DOCX_FIXTURE, _XLSX_FIXTURE, _PPTX_FIXTURE):
        shutil.copy2(fixture, target_dir / fixture.name)


def test_onedrive_scanner_content_extraction_off_yields_body_none(tmp_path: Path) -> None:
    """``content_extraction=False`` keeps the Phase 9 / box_drive shape on OneDrive too.

    Symmetric to the box_drive test: a ``.docx`` in the tree under
    the default-off configuration must surface with ``body=None`` /
    ``office_source_type=None`` so the upgrade path from Phase 11
    F4-a (box_drive only) to F4-b (also OneDrive) does not break
    existing operators.
    """
    (tmp_path / "a.docx").write_text("not-really-docx")
    (tmp_path / "b.txt").write_text("plain")

    scanner = OneDriveDriveScanner(root_path=tmp_path)
    results = {f.rel_path: f for f in scanner.scan(prior_fingerprints={})}

    assert results["a.docx"].body is None
    assert results["a.docx"].office_source_type is None
    assert results["b.txt"].body is None


def test_onedrive_scanner_content_extraction_on_extracts_office_bodies(
    tmp_path: Path,
) -> None:
    """``content_extraction=True`` + Office file → ``body`` populated.

    Smoke-tests the inherited Office hook against the real F2
    markitdown extractor. The OneDrive scanner exercises the exact
    same ``_maybe_extract`` code path as box_drive, so this test
    catches a regression where the subclass somehow re-implements
    or shadows the hook.
    """
    pytest.importorskip("markitdown")
    _copy_fixtures_into(tmp_path)

    scanner = OneDriveDriveScanner(root_path=tmp_path, content_extraction=True)
    results = {f.rel_path: f for f in scanner.scan(prior_fingerprints={})}

    assert results["sample.docx"].body is not None
    assert results["sample.docx"].office_source_type == "word_document"

    assert results["sample.xlsx"].body is not None
    assert results["sample.xlsx"].office_source_type == "excel_spreadsheet"

    assert results["sample.pptx"].body is not None
    assert results["sample.pptx"].office_source_type == "powerpoint_slide_deck"


def test_onedrive_scanner_content_extraction_failure_is_fail_safe(tmp_path: Path) -> None:
    """A corrupt Office file surfaces ``body=None`` + ``skip_reason`` (ADR-0025 §決定 (c)).

    Inherited from box_drive but pinned on the OneDrive subclass so
    a regression on either side surfaces here.
    """
    pytest.importorskip("markitdown")
    import shutil

    shutil.copy2(_DOCX_FIXTURE, tmp_path / "good.docx")
    shutil.copy2(_CORRUPT_DOCX_FIXTURE, tmp_path / "bad.docx")

    scanner = OneDriveDriveScanner(root_path=tmp_path, content_extraction=True)
    results = {f.rel_path: f for f in scanner.scan(prior_fingerprints={})}

    assert results["good.docx"].body is not None
    assert results["bad.docx"].body is None
    assert results["bad.docx"].office_source_type == "word_document"
    assert results["bad.docx"].body_skip_reason is not None
    assert results["bad.docx"].body_skip_reason.startswith("extraction failed")


# ---------------------------------------------------------------------------
# Phase 11 audit Cluster B — office_settings wire (ADR-0025 §決定 (b)/(e))
# ---------------------------------------------------------------------------


def test_onedrive_scanner_forwards_office_settings_to_extract_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``office_settings`` overrides propagate through the subclass too.

    Symmetric to the box_drive test. The OneDrive subclass shares the
    base scanner's ``_maybe_extract`` hook, so this test catches a
    regression where the subclass somehow shadows the hook with a
    stale path (= dropping the operator override silently).
    """
    from opshub.connectors.box_drive.scanner import BoxDriveScanner
    from opshub.core.config import ExcelOfficeSettings, OfficeSettings
    from opshub.core.document_extract import ExtractResult

    (tmp_path / "doc.docx").write_text("payload")

    captured: list[dict[str, Any]] = []

    def fake_extract(path: Path, **kwargs: Any) -> ExtractResult:
        captured.append({"path": path, **kwargs})
        return ExtractResult(
            body="stub",
            truncated=False,
            skip_reason=None,
            source_type="word_document",
        )

    monkeypatch.setattr("opshub.core.document_extract.extract_document", fake_extract)

    settings = OfficeSettings(
        max_file_size_mb=42,
        max_chars=123_456,
        excel=ExcelOfficeSettings(
            max_cells_per_sheet=7_000,
            max_cells_per_workbook=42_000,
        ),
    )
    scanner = OneDriveDriveScanner(
        root_path=tmp_path,
        content_extraction=True,
        office_settings=settings,
    )
    # Sanity: the subclass shares the base class type so the constructor
    # accepting the extra keyword is the MRO contract under test.
    assert isinstance(scanner, BoxDriveScanner)

    list(scanner.scan(prior_fingerprints={}))

    assert len(captured) == 1
    call = captured[0]
    assert call["max_file_bytes"] == 42 * 1024 * 1024
    assert call["max_chars"] == 123_456
    assert call["max_cells_per_sheet"] == 7_000
    assert call["max_cells_per_workbook"] == 42_000
