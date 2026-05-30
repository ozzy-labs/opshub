"""Tests for :func:`opshub.connectors.box_drive.mapper.map_scanned_file`.

Pins the Phase 9 step B2 (ADR-0019 §決定 (c)(d)(g)) mapping contract:

* ``connector_name`` = ``"box_drive"`` (distinct from Phase 7
  ``"box"``).
* ``source_type`` = :data:`SOURCE_TYPE` (``"box_drive_file"``).
* ``external_id`` = :attr:`ScannedFile.rel_path`.
* ``summary`` = ``f"path: {rel_path}"`` truncated to
  :data:`SUMMARY_MAX_CHARS` chars (ADR-0005 External Content Min).
* ``url`` = ``f"file://<abs_path>"`` derived from
  ``root_path / rel_path``.
* ``actor`` = :data:`DEFAULT_ACTOR` (``"box_drive:local"``).
* ``fingerprint`` = :attr:`ScannedFile.fingerprint` (verbatim).
* ``occurred_at`` = tz-aware UTC :class:`datetime` derived from
  ``mtime_ns / 1e9``.

The mapper requires no third-party extras (the scanner is pure
stdlib), so no ``pytest.importorskip`` guard is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from opshub.connectors.box_drive import ScannedFile
from opshub.connectors.box_drive.mapper import (
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
    """Construct a representative :class:`ScannedFile` for mapper tests.

    Defaults match a typical Box Drive PDF observation; each test
    overrides only the fields it cares about. The fingerprint is
    derived deterministically from ``(size, mtime_ns)`` to mirror
    what the scanner actually emits.
    """
    return ScannedFile(
        rel_path=rel_path,
        size=size,
        mtime_ns=mtime_ns,
        fingerprint=f"{size}:{mtime_ns}",
    )


def test_map_scanned_file_basic_fields(tmp_path: Path) -> None:
    """``ScannedFile`` → ``SourceObserved`` canonical field assembly.

    Pins every documented contract field at once: connector_name,
    source_type, external_id, title, summary, url, actor, and
    fingerprint. A regression on any of these surfaces as a single
    assertion failure with the offending field name.
    """
    scanned = _scanned(rel_path="Documents/report.pdf", size=42)

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.connector_name == "box_drive"
    assert observed.source_type == SOURCE_TYPE == "box_drive_file"
    assert observed.external_id == "Documents/report.pdf"
    assert observed.title == "Documents/report.pdf"
    assert observed.summary == "path: Documents/report.pdf"
    assert observed.url == f"file://{(tmp_path / 'Documents/report.pdf').as_posix()}"
    assert observed.actor == DEFAULT_ACTOR == "box_drive:local"
    assert observed.fingerprint == scanned.fingerprint


def test_map_scanned_file_truncates_long_path(tmp_path: Path) -> None:
    """A summary that would exceed :data:`SUMMARY_MAX_CHARS` is truncated.

    ADR-0005 caps :attr:`SourceObserved.summary` at 200 chars; the
    mapper appends a single ``"…"`` so the truncation is visible to
    operators reading recall output. The final string length is
    *exactly* :data:`SUMMARY_MAX_CHARS`.

    The rel_path used here is the maximum that fits inside the
    :attr:`SourceObserved.external_id` 200-char Pydantic cap (the
    schema rejects longer values) but still long enough that
    ``"path: " + rel_path`` (=6 chars of prefix) overflows
    :data:`SUMMARY_MAX_CHARS` and forces the mapper to truncate.
    """
    # external_id == rel_path is capped at 200 chars by the schema.
    # We use a 200-char rel_path so summary ("path: " + 200 chars =
    # 206 chars before truncation) exceeds SUMMARY_MAX_CHARS and the
    # mapper's truncation branch is exercised.
    long_rel = "a" * 200
    scanned = _scanned(rel_path=long_rel)

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.summary is not None
    assert len(observed.summary) == SUMMARY_MAX_CHARS
    assert observed.summary.endswith("…")
    assert observed.summary.startswith("path: aaaa")


def test_map_scanned_file_url_is_file_scheme_absolute(tmp_path: Path) -> None:
    """``url`` is ``file://<abs_path>`` derived from ``root_path / rel_path``.

    Phase 9 plan §4 Open Q #1 resolved this: the mapper carries a
    ``file://`` URL so ``opshub source open <id>`` can hand the path
    to ``xdg-open`` / ``open`` on the operator's host. The path is
    already absolute because the scanner is constructed with an
    absolute ``root_path``.
    """
    scanned = _scanned(rel_path="nested/dir/file.txt")

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.url is not None
    assert observed.url.startswith("file://")
    expected = f"file://{(tmp_path / 'nested/dir/file.txt').as_posix()}"
    assert observed.url == expected


def test_map_scanned_file_actor_default_is_box_drive_local(tmp_path: Path) -> None:
    """The default actor is the ADR-0019 §決定 (g) ``"box_drive:local"`` token.

    Distinct from the Phase 7 ``"connector:box"`` actor so recall
    queries / audit logs can tell apart a Web-API Box observation
    from a local-FS Box Drive scan.
    """
    scanned = _scanned()

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.actor == "box_drive:local"


def test_map_scanned_file_uses_custom_actor(tmp_path: Path) -> None:
    """The ``actor`` kwarg overrides :data:`DEFAULT_ACTOR`.

    The override exists so unit tests / future direct-construction
    paths can stamp a different provenance without monkeypatching
    the module-level default.
    """
    scanned = _scanned()

    observed = map_scanned_file(scanned, root_path=tmp_path, actor="cli:test-suite")

    assert observed.actor == "cli:test-suite"


def test_map_scanned_file_occurred_at_is_utc_aware(tmp_path: Path) -> None:
    """``mtime_ns`` → tz-aware UTC :class:`datetime` on the event.

    The base :class:`DomainEvent` enforces tz-aware UTC via its
    :func:`to_utc` validator, so a regression in the parser would
    either raise at construction time or land a naive datetime —
    both of which the assertions below cover.
    """
    # 2026-05-23T00:00:00Z in nanoseconds since epoch.
    target_dt = datetime(2026, 5, 23, 0, 0, 0, tzinfo=UTC)
    mtime_ns = int(target_dt.timestamp() * 1e9)
    scanned = _scanned(mtime_ns=mtime_ns)

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.occurred_at.tzinfo is not None
    # We do not assert sub-second precision because float arithmetic
    # may lose the last few nanoseconds; second-level equality is the
    # contract operators care about (mtime granularity on most
    # filesystems is microseconds or coarser).
    assert observed.occurred_at.replace(microsecond=0) == target_dt


def test_map_scanned_file_forwards_fingerprint(tmp_path: Path) -> None:
    """``fingerprint`` is forwarded verbatim from the scanner.

    ADR-0019 §決定 (d) makes the colon-separated ``f"{size}:{mtime_ns}"``
    string the canonical diff token. The mapper must not re-format
    it (e.g. hash it, or split on the colon) — the
    :class:`SourcesProjection` upserts the literal string into
    ``sources.fingerprint`` and the next sync's prior-fingerprint
    SELECT compares against the same literal.
    """
    scanned = ScannedFile(
        rel_path="x.txt",
        size=99,
        mtime_ns=12345,
        fingerprint="99:12345",
    )

    observed = map_scanned_file(scanned, root_path=tmp_path)

    assert observed.fingerprint == "99:12345"


def test_map_scanned_file_body_none_provenance_tagged() -> None:
    """ADR-0020: box_drive never reads file bodies, so ``body`` is ``None``.

    The observation is still external in origin, so the provenance tags
    are stamped (external / untrusted) for downstream consistency with
    the SaaS connectors.
    """
    event = map_scanned_file(_scanned(), root_path=Path("/mnt/b"))
    assert event.body is None
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"
