"""Phase 9 (ADR-0019) ``box_drive`` connector atomicity contract.

Pins the per-file Unit-of-Work guarantee for the local-filesystem-backed
Box Drive connector: each :class:`SourceObserved` + :class:`ItemEnqueued`
event pair commits atomically (one UoW per file), and a projector
failure on file N rolls back file N's pair without affecting files
1..N-1 that already committed in their own UoWs.

This mirrors the Phase 3 / 7 connector atomicity contracts pinned by
:mod:`tests.integration.test_phase7_connector_atomicity` and
:mod:`tests.integration.test_task_service_atomicity` — the box_drive
connector inherits :meth:`SourceService.observe`'s shared-transaction
shape, so the assertions are the same in shape but parameterised
against the FS-backed yield path.

The test deliberately stops short of monkeypatching the projector
mid-stream (which the Phase 7 atomicity suite already exercises for
the shared service path); it focuses on the connector-level posture
that *each file is its own UoW*. A regression that batched all
yields into a single transaction would surface here as "two files
observed but zero persisted on a third-file failure" — the existing
Phase 7 tests would not catch it because Phase 7 connectors use the
same single-yield → single-observe pattern but were written before
the box_drive connector existed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors import register_connector, unregister_all
from opshub.connectors.box_drive import ScannedFile
from opshub.connectors.box_drive.connector import BoxDriveConnector
from opshub.db.engine import create_engine_for_sqlite

if TYPE_CHECKING:
    from opshub.connectors.box_drive.scanner import BoxDriveScanner

_PathsDict = dict[str, Path]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _row_count(db_path: Path, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` against the test DB."""
    from sqlalchemy import text

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
    finally:
        engine.dispose()


def _failed_event_count(db_path: Path) -> int:
    """Count :class:`ConnectorSyncFailed` rows in the event log."""
    from sqlalchemy import text

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM events WHERE event_type = 'connector.sync_failed'")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _scanned(
    rel_path: str, *, size: int = 1, mtime_ns: int = 1_700_000_000_000_000_000
) -> ScannedFile:
    return ScannedFile(
        rel_path=rel_path,
        size=size,
        mtime_ns=mtime_ns,
        fingerprint=f"{size}:{mtime_ns}",
    )


class _StubScanner:
    """Programmable :class:`BoxDriveScanner` double for atomicity scenarios.

    Yields ``files[0..N-1]`` then raises ``error`` (when set) on the
    Nth yield boundary — the connector should have committed the
    prefix files before the exception escapes.
    """

    def __init__(
        self,
        *,
        root_path: Path,
        yields: list[ScannedFile],
        error: Exception | None = None,
    ) -> None:
        self.root_path = root_path
        self._yields = yields
        self._error = error

    def scan(self, *, prior_fingerprints: dict[str, str]) -> Iterator[ScannedFile]:
        del prior_fingerprints
        yield from self._yields
        if self._error is not None:
            raise self._error


def _install_stub_connector(
    *,
    root_path: Path,
    yields: list[ScannedFile],
    error: Exception | None = None,
) -> _StubScanner:
    """Replace the registered :class:`BoxDriveConnector` with one wired to the stub.

    Mirrors the Phase 7 ``_install_stub_connector`` helper — clears
    the registry and registers a single connector pointing at the
    stub scanner. The autouse fixture restores baseline state on
    teardown.
    """
    stub = _StubScanner(root_path=root_path, yields=yields, error=error)
    unregister_all()
    register_connector(BoxDriveConnector(scanner_factory=lambda: cast("BoxDriveScanner", stub)))
    return stub


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Restore the registry around each test.

    Without this fixture, a test that swaps :class:`BoxDriveConnector`
    for a stubbed instance would leave the stub in place for
    subsequent tests in the same process.
    """
    yield
    unregister_all()


# ----------------------------------------------------------------------
# Happy path: all yields commit individually
# ----------------------------------------------------------------------


def test_box_drive_sync_observes_each_file_atomically(
    isolated_env: _PathsDict, tmp_path: Path
) -> None:
    """Two yields → two ``sources`` rows + two ``inbox_items`` rows.

    Pins the per-file UoW shape: each yielded :class:`ScannedFile`
    produces exactly one :class:`SourceObserved` + one
    :class:`ItemEnqueued` event in a shared UoW (PR #26 / PR #47
    atomicity contract).
    """
    db_path = isolated_env["db_path"]
    root = tmp_path / "drive-root"
    root.mkdir()
    _install_stub_connector(
        root_path=root,
        yields=[
            _scanned("a.txt", size=1, mtime_ns=1_000_000_000_000),
            _scanned("dir/b.md", size=2, mtime_ns=2_000_000_000_000),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box_drive", "sync"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "synced box_drive: 2 item(s) observed" in result.stdout

    # Two files yielded → 2 sources + 2 inbox_items + 4 event rows for
    # the items + 2 bracket events (started / completed) = 6 events.
    assert _row_count(db_path, "sources") == 2
    assert _row_count(db_path, "inbox_items") == 2
    assert _row_count(db_path, "events") == 6


# ----------------------------------------------------------------------
# Partial success: prefix files commit before the scanner raises
# ----------------------------------------------------------------------


def test_box_drive_sync_persists_prefix_when_scan_raises(
    isolated_env: _PathsDict, tmp_path: Path
) -> None:
    """Scanner yields 2 files then raises → 2 sources persist + 1 sync_failed event.

    The Phase 9 connector loop calls :meth:`SourceService.observe`
    once per yielded file (one UoW each), so the two prefix yields
    land durably before the iterator raises. The CLI driver in
    :mod:`opshub.cli.connector` catches the exception, records a
    sanitised ``ConnectorSyncFailed`` event with
    ``type(exc).__name__`` only, and exits 1.

    This is the box_drive analogue of
    :func:`test_box_partial_success_persists_prefix` in the Phase 7
    atomicity suite — proving the per-file-UoW contract holds for
    the FS-backed connector too.
    """
    db_path = isolated_env["db_path"]
    root = tmp_path / "drive-root"
    root.mkdir()
    prefix = [
        _scanned("first.txt", size=1, mtime_ns=1_000_000_000_000),
        _scanned("second.txt", size=2, mtime_ns=2_000_000_000_000),
    ]
    _install_stub_connector(
        root_path=root,
        yields=prefix,
        error=RuntimeError("simulated mid-scan failure"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box_drive", "sync"])

    # CLI surface: failure path → exit 1, sanitised type name surfaces.
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "RuntimeError" in combined
    # Sanitisation: raw message must not leak through.
    assert "simulated mid-scan failure" not in combined

    # On-disk state:
    # - 2 prefix files durably committed (per-file UoW).
    # - 1 ``connector.sync_failed`` event recorded.
    # - The bracket ``ConnectorSyncCompleted`` is suppressed because
    #   the CLI driver exits before reaching ``cursor_set(sync_started=False)``.
    assert _row_count(db_path, "sources") == 2
    assert _row_count(db_path, "inbox_items") == 2
    assert _failed_event_count(db_path) == 1


# ----------------------------------------------------------------------
# Failing projector: file N's events roll back together
# ----------------------------------------------------------------------


def test_box_drive_sync_rolls_back_event_when_observe_fails(
    isolated_env: _PathsDict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Projector raises on second file → first file persisted, second fully rolled back.

    Models the shared-transaction contract from PR #26 / PR #47:
    each :meth:`SourceService.observe` call appends
    :class:`SourceObserved` + :class:`ItemEnqueued` in one UoW, so a
    projector failure on either event rolls *both* back. The first
    file's UoW already committed, so it survives.

    We monkeypatch :class:`SourcesProjection.apply` to raise on the
    second invocation; the connector loop re-raises the exception
    and the CLI driver surfaces it via ``ConnectorSyncFailed``.
    """
    from opshub.projections import sources as sources_module

    db_path = isolated_env["db_path"]
    root = tmp_path / "drive-root"
    root.mkdir()
    _install_stub_connector(
        root_path=root,
        yields=[
            _scanned("first.txt", size=1, mtime_ns=1_000_000_000_000),
            _scanned("second.txt", size=2, mtime_ns=2_000_000_000_000),
        ],
    )

    original_apply = sources_module.SourcesProjection.apply
    call_counter = MagicMock()

    def _maybe_failing_apply(
        self: sources_module.SourcesProjection, conn: object, event: object
    ) -> None:
        call_counter()
        # Only fail when the projection is applying a SourceObserved
        # (the second file's commit). The ``ItemEnqueued`` events go
        # through the InboxProjection, not this one — we count the
        # ``SourceObserved`` calls specifically so we can fail the
        # second file's UoW deterministically.
        from opshub.domain.events import SourceObserved

        if isinstance(event, SourceObserved):
            if event.external_id == "second.txt":
                raise RuntimeError("simulated projection failure")
        original_apply(self, conn, event)  # type: ignore[arg-type]

    monkeypatch.setattr(
        sources_module.SourcesProjection,
        "apply",
        _maybe_failing_apply,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["box_drive", "sync"])
    assert result.exit_code == 1

    # First file's UoW committed → exactly one sources / inbox row.
    # Second file's UoW rolled back → no second row in either table.
    assert _row_count(db_path, "sources") == 1
    assert _row_count(db_path, "inbox_items") == 1
    # The first file's two events + the bracket Started + the failed
    # second-file SourceObserved was rolled back, so we expect:
    # Started + SourceObserved(first) + ItemEnqueued(first) + ConnectorSyncFailed
    # = 4 events.
    # We do not pin the exact count too tightly (other projectors may
    # write additional bookkeeping); we just assert the
    # ``connector.sync_failed`` event is present.
    assert _failed_event_count(db_path) == 1
