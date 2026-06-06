"""Tests for :class:`opshub.connectors.onedrive_drive.connector.OneDriveDriveConnector`.

Pins the ADR-0019 §(j) per-connector overrides:

* ``name`` = ``"onedrive_drive"`` (registry / CLI dispatch key).
* Settings come from ``connectors.onedrive_drive``.
* Platform defaults from :func:`onedrive_drive_default_root_path`.
* Prior fingerprints filtered to
  ``connector_name = 'onedrive_drive'``.
* ``content_extraction`` is threaded into the scanner.

Tests mirror the structure of the box_drive connector suite so a
regression on either side is caught in symmetric fashion.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from opshub.connectors.context import ConnectorContext
from opshub.connectors.onedrive_drive import ScannedFile
from opshub.connectors.onedrive_drive.connector import OneDriveDriveConnector
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from opshub.connectors.onedrive_drive.scanner import OneDriveDriveScanner

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double recording ``observe`` calls (mirrors box_drive's)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.engine: Any = None
        self._engine: Any = None

    def observe(
        self,
        *,
        connector_name: str,
        external_id: str,
        source_type: str,
        title: str,
        url: str | None = None,
        summary: str | None = None,
        fingerprint: str | None = None,
        body: str | None = None,
        provenance_origin: str | None = None,
        provenance_trust: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "connector_name": connector_name,
                "external_id": external_id,
                "source_type": source_type,
                "title": title,
                "url": url,
                "summary": summary,
                "fingerprint": fingerprint,
                "body": body,
                "provenance_origin": provenance_origin,
                "provenance_trust": provenance_trust,
            }
        )


def _scanned(
    *,
    rel_path: str = "a.txt",
    size: int = 5,
    mtime_ns: int = 1_700_000_000_000_000_000,
) -> ScannedFile:
    return ScannedFile(
        rel_path=rel_path,
        size=size,
        mtime_ns=mtime_ns,
        fingerprint=f"{size}:{mtime_ns}",
    )


class _StubScanner:
    """Programmable scanner double for connector unit tests."""

    def __init__(
        self,
        *,
        root_path: Path,
        yields: list[ScannedFile] | None = None,
    ) -> None:
        self.root_path = root_path
        self._yields = yields or []
        self.received_prior: dict[str, str] | None = None

    def scan(self, *, prior_fingerprints: dict[str, str]) -> Iterator[ScannedFile]:
        self.received_prior = dict(prior_fingerprints)
        yield from self._yields


def _context(service: _RecordingSourceService) -> ConnectorContext:
    return ConnectorContext(
        source_service=service,
        cursor_value=None,
        secrets=None,
        logger=MagicMock(),
    )


def _connector_with_stub(scanner: _StubScanner) -> OneDriveDriveConnector:
    return OneDriveDriveConnector(
        scanner_factory=lambda: cast("OneDriveDriveScanner", scanner),
    )


# ---------------------------------------------------------------------- name


def test_connector_name_is_onedrive_drive() -> None:
    """Registry / CLI dispatch key is exactly ``"onedrive_drive"``."""
    assert OneDriveDriveConnector.name == "onedrive_drive"
    assert OneDriveDriveConnector().name == "onedrive_drive"


# ---------------------------------------------------------------------- settings


def test_sync_uses_explicit_root_path_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit ``[connectors.onedrive_drive] root_path`` wins over the platform default."""
    (tmp_path / "hello.txt").write_text("hi")
    monkeypatch.setenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH", str(tmp_path))

    service = _RecordingSourceService()
    result = OneDriveDriveConnector().sync(_context(service))

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "hello.txt"
    assert service.calls[0]["connector_name"] == "onedrive_drive"
    assert service.calls[0]["url"] is not None
    assert service.calls[0]["url"].endswith("/hello.txt")
    assert str(tmp_path) in service.calls[0]["url"]


def test_sync_falls_back_to_platform_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``root_path = None`` → :func:`onedrive_drive_default_root_path` is consulted."""
    (tmp_path / "default-root.txt").write_text("x")
    monkeypatch.delenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH", raising=False)
    monkeypatch.setattr(
        "opshub.core.platform.onedrive_drive_default_root_path",
        lambda: tmp_path,
    )

    service = _RecordingSourceService()
    result = OneDriveDriveConnector().sync(_context(service))

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "default-root.txt"


def test_sync_raises_config_error_when_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux native (helper returns ``None``) + no override → :class:`ConfigError`.

    Error message points at ``docs/onedrive-drive-setup.md`` (not the
    box_drive setup doc) so operators land on the right page.
    """
    monkeypatch.delenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH", raising=False)
    monkeypatch.setattr(
        "opshub.core.platform.onedrive_drive_default_root_path",
        lambda: None,
    )

    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="onedrive-drive-setup"):
        OneDriveDriveConnector().sync(_context(service))


def test_sync_raises_config_error_when_root_path_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``root_path`` set but absent on disk → :class:`ConfigError`."""
    missing = tmp_path / "not-a-real-mount"
    monkeypatch.setenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH", str(missing))

    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="does not exist"):
        OneDriveDriveConnector().sync(_context(service))


# ---------------------------------------------------------------------- sync flow


def test_sync_observes_each_yielded_file(tmp_path: Path) -> None:
    """Per-file pipeline: 3 yields → 3 ``observe`` calls."""
    files = [
        _scanned(rel_path="a.txt", size=1, mtime_ns=1_000_000_000_000),
        _scanned(rel_path="dir/b.md", size=2, mtime_ns=2_000_000_000_000),
        _scanned(rel_path="dir/c.md", size=3, mtime_ns=3_000_000_000_000),
    ]
    scanner = _StubScanner(root_path=tmp_path, yields=files)
    connector = _connector_with_stub(scanner)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.observed_count == 3
    assert len(service.calls) == 3
    assert [c["external_id"] for c in service.calls] == [
        "a.txt",
        "dir/b.md",
        "dir/c.md",
    ]
    assert all(c["connector_name"] == "onedrive_drive" for c in service.calls)
    assert all(c["source_type"] == "onedrive_drive_file" for c in service.calls)
    assert [c["fingerprint"] for c in service.calls] == [
        "1:1000000000000",
        "2:2000000000000",
        "3:3000000000000",
    ]


def test_sync_with_no_yields_returns_zero_observed(tmp_path: Path) -> None:
    """Empty scan → 0 observed, no observe calls, but cursor still set."""
    scanner = _StubScanner(root_path=tmp_path, yields=[])
    connector = _connector_with_stub(scanner)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.observed_count == 0
    assert service.calls == []
    assert result.new_cursor is not None


def test_sync_returns_iso_timestamp_cursor(tmp_path: Path) -> None:
    """``SyncResult.new_cursor`` is parseable as ISO-8601 with tzinfo."""
    from datetime import datetime

    scanner = _StubScanner(root_path=tmp_path, yields=[])
    connector = _connector_with_stub(scanner)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.new_cursor is not None
    parsed = datetime.fromisoformat(result.new_cursor)
    assert parsed.tzinfo is not None


def test_sync_threads_prior_fingerprints_filtered_by_connector_name(
    tmp_path: Path,
) -> None:
    """Prior fingerprints filter to ``connector_name = 'onedrive_drive'`` rows.

    A row from a different connector (``box_drive``) must NOT leak
    into the OneDrive ``prior_fingerprints`` map — otherwise the two
    local-FS connectors would falsely suppress each other's events.
    """
    from datetime import datetime

    from sqlalchemy import create_engine, insert

    from opshub.db.schema import metadata
    from opshub.projections.sources import sources_table

    engine = create_engine("sqlite:///:memory:")
    try:
        metadata.create_all(engine)
        now = datetime(2026, 5, 23, tzinfo=UTC)
        with engine.begin() as conn:
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000AA",
                    connector_name="onedrive_drive",
                    external_id="existing.txt",
                    source_type="onedrive_drive_file",
                    title="existing.txt",
                    url=None,
                    summary="path: existing.txt",
                    observed_at=now,
                    updated_at=now,
                    fingerprint="42:99",
                )
            )
            # box_drive row with the same external_id must NOT leak.
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000BB",
                    connector_name="box_drive",
                    external_id="existing.txt",
                    source_type="box_drive_file",
                    title="existing.txt",
                    url=None,
                    summary="path: existing.txt",
                    observed_at=now,
                    updated_at=now,
                    fingerprint="should-not-leak",
                )
            )
            # onedrive_drive row with NULL fingerprint must be skipped.
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000CC",
                    connector_name="onedrive_drive",
                    external_id="legacy.txt",
                    source_type="onedrive_drive_file",
                    title="legacy.txt",
                    url=None,
                    summary=None,
                    observed_at=now,
                    updated_at=now,
                    fingerprint=None,
                )
            )

        scanner = _StubScanner(root_path=tmp_path, yields=[])
        connector = _connector_with_stub(scanner)

        service = _RecordingSourceService()
        service.engine = engine
        connector.sync(_context(service))

        assert scanner.received_prior == {"existing.txt": "42:99"}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------- shared excludes


def test_sync_threads_shared_excludes_into_scanner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): shared ``excludes.yaml`` reaches the scanner as ``ExcludeRules``.

    Post-#470 the connector loads :class:`ExcludeRules` from
    ``excludes.yaml`` and hands the value object to
    :class:`OneDriveDriveScanner` (no more inline ``exclude_globs``
    merge). Symmetric with the box_drive sibling.
    """
    secrets_dir = tmp_path / "drive" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "key.pem").write_text("PRIVATE")
    (tmp_path / "drive" / "report.md").write_text("public report body")
    monkeypatch.setenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH", str(tmp_path / "drive"))

    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(
        "paths:\n  - '**/secrets/**'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)

    service = _RecordingSourceService()
    result = OneDriveDriveConnector().sync(_context(service))

    assert result.observed_count == 1
    assert len(service.calls) == 1
    assert service.calls[0]["external_id"] == "report.md"
    assert all("secret" not in str(c["external_id"]) for c in service.calls)


# ---------------------------------------------------------------------- content_extraction


def test_sync_threads_content_extraction_flag_to_scanner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``[connectors.onedrive_drive] content_extraction = true`` reaches the scanner.

    Mirrors the box_drive F4-a assertion: settings propagate through
    the connector to the scanner constructor without being silently
    dropped.
    """
    monkeypatch.setenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH", str(tmp_path))
    monkeypatch.setenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__CONTENT_EXTRACTION", "true")

    captured: dict[str, Any] = {}
    from opshub.connectors.onedrive_drive import scanner as scanner_mod

    real_init = scanner_mod.OneDriveDriveScanner.__init__

    def capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(scanner_mod.OneDriveDriveScanner, "__init__", capturing_init)

    service = _RecordingSourceService()
    OneDriveDriveConnector().sync(_context(service))

    assert captured.get("content_extraction") is True


def test_sync_content_extraction_default_false_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default ``content_extraction = False`` reaches the scanner unchanged."""
    monkeypatch.setenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__ROOT_PATH", str(tmp_path))
    monkeypatch.delenv("OPSHUB_CONNECTORS__ONEDRIVE_DRIVE__CONTENT_EXTRACTION", raising=False)

    captured: dict[str, Any] = {}
    from opshub.connectors.onedrive_drive import scanner as scanner_mod

    real_init = scanner_mod.OneDriveDriveScanner.__init__

    def capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(scanner_mod.OneDriveDriveScanner, "__init__", capturing_init)

    service = _RecordingSourceService()
    OneDriveDriveConnector().sync(_context(service))

    assert captured.get("content_extraction") is False


def test_sync_office_observe_call_carries_body_and_source_type(
    tmp_path: Path,
) -> None:
    """End-to-end (stub): Office ``ScannedFile`` → SourceObserved with body + Office source_type."""
    office_file = ScannedFile(
        rel_path="docs/spec.docx",
        size=4096,
        mtime_ns=1_700_000_000_000_000_000,
        fingerprint="4096:1700000000000000000",
        body="# Spec\n\nFull body.",
        office_source_type="word_document",
        body_truncated=False,
        body_skip_reason=None,
    )
    plain_file = ScannedFile(
        rel_path="notes/todo.txt",
        size=42,
        mtime_ns=1_700_000_000_000_000_000,
        fingerprint="42:1700000000000000000",
        body=None,
        office_source_type=None,
    )

    scanner = _StubScanner(root_path=tmp_path, yields=[office_file, plain_file])
    connector = _connector_with_stub(scanner)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.observed_count == 2
    office_call = next(c for c in service.calls if c["external_id"] == "docs/spec.docx")
    plain_call = next(c for c in service.calls if c["external_id"] == "notes/todo.txt")

    assert office_call["source_type"] == "word_document"
    assert office_call["body"] == "# Spec\n\nFull body."
    assert office_call["connector_name"] == "onedrive_drive"
    assert office_call["provenance_origin"] == "external"
    assert office_call["provenance_trust"] == "untrusted"

    assert plain_call["source_type"] == "onedrive_drive_file"
    assert plain_call["body"] is None
    assert plain_call["connector_name"] == "onedrive_drive"
    assert plain_call["provenance_origin"] == "external"
    assert plain_call["provenance_trust"] == "untrusted"


# ---------------------------------------------------------------------- registry


def test_onedrive_drive_subpackage_registers_connector() -> None:
    """Importing :mod:`opshub.connectors.onedrive_drive` registers it.

    Side-effect registration makes ``opshub connector sync
    onedrive_drive`` work without an explicit ``import`` in the CLI
    driver beyond the lazy ``import opshub.connectors.onedrive_drive``.
    """
    import importlib

    import opshub.connectors.onedrive_drive
    from opshub.connectors import discover_connectors, unregister_all

    unregister_all()
    importlib.reload(opshub.connectors.onedrive_drive)

    names = {c.name for c in discover_connectors()}
    assert "onedrive_drive" in names
