"""Tests for :func:`opshub.connectors.onedrive_drive.mapper.map_scanned_file`.

Pins the ADR-0019 §(j) per-connector identity overrides:

* ``connector_name`` = ``"onedrive_drive"`` (distinct from
  ``"box_drive"``).
* ``source_type`` = :data:`SOURCE_TYPE` (``"onedrive_drive_file"``)
  for non-Office files; Office discriminator override carries from
  :attr:`ScannedFile.office_source_type` (shared ADR-0025 §決定 (d)
  pattern).
* ``actor`` = :data:`DEFAULT_ACTOR` (``"onedrive_drive:local"``).
* ``url`` = ``file://<abs_path>`` derived from ``root_path / rel_path``.
* ``provenance_origin`` = ``"external"``, ``provenance_trust`` =
  ``"untrusted"`` (ADR-0020 §(e), matches box_drive).

Mapping logic is structurally identical to the box_drive mapper, but
the identity-related constants must NOT collide — this suite is the
contract that catches a copy-paste regression where the OneDrive
mapper accidentally stamps ``"box_drive"`` somewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from opshub.connectors.onedrive_drive import ScannedFile
from opshub.connectors.onedrive_drive.mapper import (
    DEFAULT_ACTOR,
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    map_scanned_file,
)


def _scanned(
    *,
    rel_path: str = "Documents/report.pdf",
    size: int = 1024,
    mtime_ns: int = 1_700_000_000_000_000_000,
) -> ScannedFile:
    """Construct a representative :class:`ScannedFile` for OneDrive mapper tests."""
    return ScannedFile(
        rel_path=rel_path,
        size=size,
        mtime_ns=mtime_ns,
        fingerprint=f"{size}:{mtime_ns}",
    )


def test_map_scanned_file_basic_fields(tmp_path: Path) -> None:
    """Canonical field assembly for a non-Office file."""
    scanned = _scanned(rel_path="Documents/report.pdf", size=42)

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.connector_name == "onedrive_drive"
    assert observed.source_type == SOURCE_TYPE == "onedrive_drive_file"
    assert observed.external_id == "Documents/report.pdf"
    assert observed.title == "Documents/report.pdf"
    assert observed.summary == "path: Documents/report.pdf"
    assert observed.url == f"file://{(tmp_path / 'Documents/report.pdf').as_posix()}"
    assert observed.actor == DEFAULT_ACTOR == "onedrive_drive:local"
    assert observed.fingerprint == scanned.fingerprint


def test_map_scanned_file_truncates_long_path(tmp_path: Path) -> None:
    """A summary exceeding :data:`SUMMARY_MAX_CHARS` is truncated with ``"…"``."""
    long_rel = "a" * 200
    scanned = _scanned(rel_path=long_rel)

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.summary is not None
    assert len(observed.summary) == SUMMARY_MAX_CHARS
    assert observed.summary.endswith("…")
    assert observed.summary.startswith("path: aaaa")


def test_map_scanned_file_url_is_file_scheme_absolute(tmp_path: Path) -> None:
    """``url`` is ``file://<abs_path>`` (same shape as box_drive)."""
    scanned = _scanned(rel_path="nested/dir/file.txt")

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.url is not None
    assert observed.url.startswith("file://")
    assert observed.url == f"file://{(tmp_path / 'nested/dir/file.txt').as_posix()}"


def test_map_scanned_file_actor_default_is_onedrive_drive_local(tmp_path: Path) -> None:
    """Default actor is ``onedrive_drive:local`` (distinct from box_drive's actor)."""
    scanned = _scanned()

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.actor == "onedrive_drive:local"
    # Pin the negative too — a regression that copy-pasted from
    # box_drive must fail this assertion.
    assert observed.actor != "box_drive:local"


def test_map_scanned_file_uses_custom_actor(tmp_path: Path) -> None:
    """The ``actor`` kwarg overrides :data:`DEFAULT_ACTOR`."""
    scanned = _scanned()

    observed = map_scanned_file(scanned, root_path=tmp_path, actor="cli:test-suite")

    assert observed.actor == "cli:test-suite"


def test_map_scanned_file_occurred_at_is_utc_aware(tmp_path: Path) -> None:
    """``mtime_ns`` → tz-aware UTC :class:`datetime` on the event."""
    target_dt = datetime(2026, 5, 23, 0, 0, 0, tzinfo=UTC)
    mtime_ns = int(target_dt.timestamp() * 1e9)
    scanned = _scanned(mtime_ns=mtime_ns)

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.occurred_at.tzinfo is not None
    assert observed.occurred_at.replace(microsecond=0) == target_dt


def test_map_scanned_file_forwards_fingerprint(tmp_path: Path) -> None:
    """``fingerprint`` is forwarded verbatim (ADR-0019 §決定 (d) shared)."""
    scanned = ScannedFile(
        rel_path="x.txt",
        size=99,
        mtime_ns=12345,
        fingerprint="99:12345",
    )

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.fingerprint == "99:12345"


def test_map_scanned_file_body_equals_summary_provenance_tagged(tmp_path: Path) -> None:
    """epic #470 / issue #481: stat-only paths emit ``body = summary`` for OneDrive too.

    Mirrors box_drive — local-FS connectors do not read bodies by
    default (ADR-0019 §不変条件 (b)). The metadata-only rule
    (ADR-0010 §不変条件) substitutes the composed ``"path: <rel_path>"``
    summary as the body so the :class:`SourceObserved.body`
    ``min_length=1`` invariant holds without violating the
    no-``open()`` contract.
    """
    event = map_scanned_file(_scanned(), root_path=tmp_path)
    assert event.body == event.summary
    assert event.body and event.body.strip()
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


def test_map_scanned_file_with_office_source_type_overrides_default(
    tmp_path: Path,
) -> None:
    """Office discriminator override (ADR-0025 §決定 (d)) carries over to OneDrive."""
    scanned = ScannedFile(
        rel_path="docs/report.docx",
        size=1024,
        mtime_ns=1_700_000_000_000_000_000,
        fingerprint="1024:1700000000000000000",
        body="# Report\n\nExtracted body.",
        office_source_type="word_document",
        body_truncated=False,
        body_skip_reason=None,
    )

    event = map_scanned_file(scanned, root_path=tmp_path)

    assert event.source_type == "word_document"
    assert event.body == "# Report\n\nExtracted body."
    assert event.connector_name == "onedrive_drive"
    assert event.external_id == "docs/report.docx"
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


def test_map_scanned_file_office_extraction_failure_falls_back_to_summary(
    tmp_path: Path,
) -> None:
    """ADR-0025 §決定 (c) fail-safe carries over; epic #470 / #481 falls back to summary."""
    scanned = ScannedFile(
        rel_path="docs/big.xlsx",
        size=999_999_999,
        mtime_ns=1_700_000_000_000_000_000,
        fingerprint="999999999:1700000000000000000",
        body=None,
        office_source_type="excel_spreadsheet",
        body_truncated=False,
        body_skip_reason="file too large",
    )

    event = map_scanned_file(scanned, root_path=tmp_path)

    assert event.body == event.summary
    assert event.body and event.body.strip()
    assert event.source_type == "excel_spreadsheet"
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"
