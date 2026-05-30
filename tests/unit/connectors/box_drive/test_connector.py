"""Tests for :class:`opshub.connectors.box_drive.connector.BoxDriveConnector`.

Pins the Phase 9 step B2 (ADR-0019) connector-level contract
independently of the end-to-end CLI lifecycle:

1. ``name`` matches the registry key the CLI dispatches on
   (``"box_drive"``).
2. Settings resolution: explicit ``root_path`` wins over the
   platform default; ``None`` + WSL2 → ``/mnt/b``; ``None`` + Linux
   native → :class:`ConfigError`.
3. ``root_path`` missing on disk → :class:`ConfigError` (re-raised
   verbatim from the scanner constructor).
4. Prior fingerprints are hydrated from the ``sources`` projection
   via the :class:`SourceService` engine handle.
5. Scanner yields → :meth:`SourceService.observe` is called per
   file with the ``fingerprint=`` keyword.
6. No yields → ``observed_count = 0`` + no ``observe`` calls.
7. Multiple yields → one ``observe`` call per file in scan order.

Tests use a fake source-service double + a scanner factory injection
so no real SQLite engine / filesystem is required for the
connector-internal logic (the integration suite covers the wired
end-to-end path).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from opshub.connectors.box_drive import ScannedFile
from opshub.connectors.box_drive.connector import BoxDriveConnector
from opshub.connectors.context import ConnectorContext
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from opshub.connectors.box_drive.scanner import BoxDriveScanner

# ---------------------------------------------------------------------- helpers


class _RecordingSourceService:
    """Test double for :class:`SourceService` that records ``observe`` calls.

    Mirrors the keyword-only signature used by the real service so a
    drift on argument names (notably the Phase 9 ``fingerprint=``
    keyword added in step A2) trips :class:`TypeError` immediately.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.engine: Any = None
        # ``_engine`` mirrors the private attribute the connector's
        # ``_load_prior_fingerprints`` falls back to when no public
        # ``engine`` property exists. Default ``None`` means
        # "first sync — every file gets yielded".
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
    """Construct a representative :class:`ScannedFile` for connector tests."""
    return ScannedFile(
        rel_path=rel_path,
        size=size,
        mtime_ns=mtime_ns,
        fingerprint=f"{size}:{mtime_ns}",
    )


class _StubScanner:
    """Programmable :class:`BoxDriveScanner` double.

    The connector reaches the scanner through the
    :class:`BoxDriveConnector` factory seam and only calls
    :meth:`scan` + reads :attr:`root_path` on it, so structural
    typing is sufficient — we deliberately do NOT subclass the real
    scanner so the test stays decoupled from its internals.
    """

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


def _connector_with_stub(scanner: _StubScanner) -> BoxDriveConnector:
    """Build a :class:`BoxDriveConnector` wired to a stub scanner.

    The :class:`BoxDriveConnector` constructor's ``scanner_factory``
    is typed as ``() -> BoxDriveScanner``; the stub is structurally
    compatible (duck-typed) but not a nominal subclass, so a
    :func:`cast` keeps pyright honest about the deliberate test
    seam.
    """
    return BoxDriveConnector(
        scanner_factory=lambda: cast("BoxDriveScanner", scanner),
    )


# ---------------------------------------------------------------------- name


def test_connector_name_is_box_drive() -> None:
    """The registry / CLI dispatch key must be exactly ``"box_drive"``.

    Distinct from the Phase 7 ``"box"`` connector so the two can
    coexist under one operator install (ADR-0019 §関連).
    """
    assert BoxDriveConnector.name == "box_drive"
    assert BoxDriveConnector().name == "box_drive"


# ---------------------------------------------------------------------- settings


def test_sync_uses_explicit_root_path_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit ``[connectors.box_drive] root_path`` wins over the platform default.

    The real production path runs through
    :class:`OpsHubSettings` → :func:`box_drive_default_root_path`.
    We patch the env var so pydantic-settings picks up
    ``tmp_path`` as the explicit override, and verify the resulting
    scanner is constructed against it.
    """
    (tmp_path / "hello.txt").write_text("hi")
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH", str(tmp_path))

    service = _RecordingSourceService()
    result = BoxDriveConnector().sync(_context(service))

    # The scanner ran and emitted exactly one file (no prior
    # fingerprints because the fake service has no engine).
    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "hello.txt"
    # ``url`` reflects the explicit root path.
    assert service.calls[0]["url"] is not None
    assert service.calls[0]["url"].endswith("/hello.txt")
    assert str(tmp_path) in service.calls[0]["url"]


def test_sync_falls_back_to_platform_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``root_path = None`` → :func:`box_drive_default_root_path` is consulted.

    The platform helper is monkeypatched to return ``tmp_path`` so
    the scanner's existence check passes; the real helper would
    return ``/mnt/b`` on WSL2 / ``~/Box`` on macOS / ``None`` on
    Linux native. The contract is "if settings.root_path is None,
    the connector calls the helper" — we pin that contract here.
    """
    (tmp_path / "default-root.txt").write_text("x")
    # Clear any env override that might leak from an earlier test.
    monkeypatch.delenv("OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH", raising=False)
    monkeypatch.setattr(
        "opshub.core.platform.box_drive_default_root_path",
        lambda: tmp_path,
    )

    service = _RecordingSourceService()
    result = BoxDriveConnector().sync(_context(service))

    assert result.observed_count == 1
    assert service.calls[0]["external_id"] == "default-root.txt"


def test_sync_raises_config_error_when_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux native (helper returns ``None``) + no override → :class:`ConfigError`.

    ADR-0019 §決定 (f) trailing note: callers must fail-fast with a
    pointer to ``docs/box-drive-setup.md`` when no default exists.
    The connector does exactly that here.
    """
    monkeypatch.delenv("OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH", raising=False)
    monkeypatch.setattr(
        "opshub.core.platform.box_drive_default_root_path",
        lambda: None,
    )

    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="box-drive-setup"):
        BoxDriveConnector().sync(_context(service))


def test_sync_raises_config_error_when_root_path_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``root_path`` set but absent on disk → :class:`ConfigError`.

    The scanner constructor raises :class:`ConfigError` with the
    setup-doc pointer; the connector propagates verbatim.
    """
    missing = tmp_path / "not-a-real-mount"
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH", str(missing))

    service = _RecordingSourceService()
    with pytest.raises(ConfigError, match="does not exist"):
        BoxDriveConnector().sync(_context(service))


# ---------------------------------------------------------------------- sync flow


def test_sync_observes_each_yielded_file(tmp_path: Path) -> None:
    """Scanner yields three files → three ``observe`` calls with fingerprint.

    Pins the per-file pipeline (yield → mapper → observe) and the
    Phase 9 step A2 ``fingerprint=`` keyword threading. The
    ``connector_name`` / ``source_type`` are also pinned so a
    regression that drops the keyword splat surfaces here.
    """
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
    assert all(c["connector_name"] == "box_drive" for c in service.calls)
    assert all(c["source_type"] == "box_drive_file" for c in service.calls)
    assert [c["fingerprint"] for c in service.calls] == [
        "1:1000000000000",
        "2:2000000000000",
        "3:3000000000000",
    ]


def test_sync_with_no_yields_returns_zero_observed(tmp_path: Path) -> None:
    """Scanner yields nothing (every file matches prior_fingerprints) → 0 observed.

    Mirrors the noise-suppression contract pinned by the scanner
    tests: an unchanged workspace must not produce
    :class:`SourceObserved` event noise.
    """
    scanner = _StubScanner(root_path=tmp_path, yields=[])
    connector = _connector_with_stub(scanner)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.observed_count == 0
    assert service.calls == []
    # ``new_cursor`` is still set — it is an informational timestamp.
    assert result.new_cursor is not None


def test_sync_returns_iso_timestamp_cursor(tmp_path: Path) -> None:
    """``SyncResult.new_cursor`` is an ISO-8601 UTC timestamp string.

    Phase 9 MVP treats the cursor as informational only (the
    scanner's fingerprint diff is the actual resume mechanism). We
    still pin the *shape* so a regression that breaks the
    :class:`ConnectorCursorsProjection` upsert surfaces immediately.
    """
    from datetime import datetime

    scanner = _StubScanner(root_path=tmp_path, yields=[])
    connector = _connector_with_stub(scanner)

    service = _RecordingSourceService()
    result = connector.sync(_context(service))

    assert result.new_cursor is not None
    # ``datetime.fromisoformat`` accepts the value the connector
    # writes; any drift on format (e.g. dropping the tz suffix)
    # would raise here.
    parsed = datetime.fromisoformat(result.new_cursor)
    assert parsed.tzinfo is not None


def test_sync_threads_prior_fingerprints_from_engine(
    tmp_path: Path,
) -> None:
    """Prior fingerprints are SELECTed from the ``sources`` projection.

    We stand up a real SQLite engine with the ``sources`` table,
    seed one row, and verify the connector hydrates the
    ``prior_fingerprints`` dict from it before delegating to the
    scanner. This pins the SELECT path
    (``connector_name = 'box_drive'`` + ``fingerprint IS NOT NULL``
    skip) which is otherwise easy to break with a column drift.
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
                    connector_name="box_drive",
                    external_id="existing.txt",
                    source_type="box_drive_file",
                    title="existing.txt",
                    url=None,
                    summary="path: existing.txt",
                    observed_at=now,
                    updated_at=now,
                    fingerprint="42:99",
                )
            )
            # Row from a *different* connector — must not leak into
            # the prior_fingerprints dict.
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000BB",
                    connector_name="box",
                    external_id="evt-foreign",
                    source_type="box_event",
                    title="ITEM_CREATE: foreign",
                    url=None,
                    summary=None,
                    observed_at=now,
                    updated_at=now,
                    fingerprint="should-not-leak",
                )
            )
            # Row from box_drive with NULL fingerprint — must be
            # skipped (legacy / pre-migration row).
            conn.execute(
                insert(sources_table).values(
                    id="01H000000000000000000000CC",
                    connector_name="box_drive",
                    external_id="legacy.txt",
                    source_type="box_drive_file",
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

        # Only the matching connector_name row with a non-NULL
        # fingerprint surfaces in the prior map.
        assert scanner.received_prior == {"existing.txt": "42:99"}
    finally:
        engine.dispose()


def test_sync_uses_private_engine_attribute_as_fallback(tmp_path: Path) -> None:
    """The ``_engine`` private attribute is the documented fallback.

    :class:`SourceService` exposes the engine via ``self._engine``
    (the public ``engine`` parameter on the constructor; pydantic /
    dataclass conventions vary across projects so we tolerate both).
    The connector tries ``engine`` first then ``_engine``; absence
    of both means "first sync, no prior fingerprints".
    """
    scanner = _StubScanner(root_path=tmp_path, yields=[])
    connector = _connector_with_stub(scanner)

    service = _RecordingSourceService()
    # Set only the private attribute. The connector must still find
    # it (real SourceService uses the same name). Assigning via
    # ``setattr`` avoids pyright's ``reportPrivateUsage`` warning
    # for the deliberate name-pin under test.
    setattr(service, "_engine", None)  # noqa: B010
    connector.sync(_context(service))

    assert scanner.received_prior == {}


# ---------------------------------------------------------------------- registry


def test_sync_merges_shared_excludes_paths_into_scanner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0020 §(b): shared ``excludes.yaml`` ``paths`` reach the scanner.

    The connector merges its inline ``[connectors.box_drive] exclude_globs``
    with the shared ``paths`` selector from ``excludes.yaml`` and hands
    the combined list to :class:`BoxDriveScanner`. A file matching the
    shared rule must be skipped before any ``observe`` call lands.

    Pins the ``merged_with_paths``-equivalent in-connector merger so a
    regression that silently drops the shared list does not get past
    review (Phase 10 audit Cluster 3 §A).
    """
    # Set up a tiny box-drive root with one "secret" file and one safe file.
    secrets_dir = tmp_path / "drive" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "key.pem").write_text("PRIVATE")
    (tmp_path / "drive" / "report.md").write_text("public report body")
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH", str(tmp_path / "drive"))

    # Point ``load_excludes`` at a config dir whose ``excludes.yaml``
    # excludes anything under ``secrets/``.
    cfg_dir = tmp_path / "opshub-config"
    cfg_dir.mkdir()
    (cfg_dir / "excludes.yaml").write_text(
        "paths:\n  - '**/secrets/**'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("opshub.core.excludes.default_config_dir", lambda: cfg_dir)
    # ``OpsHubSettings.config_dir`` is the resolution path the connector
    # passes into ``load_excludes(config_dir=...)``; mirror it so both
    # call sites resolve to the same place.
    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(cfg_dir))

    service = _RecordingSourceService()
    result = BoxDriveConnector().sync(_context(service))

    # Only the non-secret file is observed; the scanner short-circuits
    # on the shared glob before the connector ever sees it.
    assert result.observed_count == 1
    assert len(service.calls) == 1
    assert service.calls[0]["external_id"] == "report.md"
    assert all("secret" not in str(c["external_id"]) for c in service.calls)


def test_box_drive_subpackage_registers_connector() -> None:
    """Importing :mod:`opshub.connectors.box_drive` registers the connector.

    Side-effect registration is the contract that makes ``opshub
    connector sync box_drive`` resolve through
    :func:`discover_connectors` without an explicit import in the
    CLI driver beyond the lazy ``import opshub.connectors.box_drive``.
    Mirrors the Phase 3 / 7 precedents.
    """
    import importlib

    import opshub.connectors.box_drive
    from opshub.connectors import discover_connectors, unregister_all

    unregister_all()
    importlib.reload(opshub.connectors.box_drive)

    names = {c.name for c in discover_connectors()}
    assert "box_drive" in names
