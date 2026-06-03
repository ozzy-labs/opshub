"""Phase 9 end-to-end lifecycle test (Local-filesystem-backed Connector closeout).

Drives the Phase 9 ``box_drive`` connector pipeline through the shipped
CLI surface using a temporary directory as ``root_path`` — no real Box
Drive client / mount point is required. Pattern mirrors
:mod:`tests.integration.test_phase7_lifecycle` (Phase 7 closeout): a
single happy-path lifecycle test that walks ``connector list`` →
``connector sync box_drive`` twice across a 2-pass scenario so the
ADR-0019 §決定 (c)(d)(e) contracts (rel_path identity + fingerprint
diff detection + deletion-not-tracked) round-trip through the shared
Phase 3 ``SourceService`` + Phase 1 event store + Phase 9 step A2
``sources.fingerprint`` projection column.

What this pins
--------------

- ``opshub connectors`` surfaces ``box_drive`` so operators
  discover the connector even before they enable it (ADR-0019 §決定
  (a) — actionable presence regardless of ``enabled`` flag).
- **Pass 1** — three files exist in ``root_path`` → three
  :class:`SourceObserved` events committed, three ``sources`` rows
  persist with ``source_type="box_drive_file"`` and a populated
  ``fingerprint`` (Phase 9 step A2). One :class:`ItemEnqueued` per
  file lands in the ``inbox_items`` projection (PR #26 atomic UoW
  contract).
- **Pass 2** — one file is modified (mtime advanced), one file is
  added, one file is deleted. Only the modified + added files surface
  as new :class:`SourceObserved` events (ADR-0019 §決定 (d)
  fingerprint diff). The deleted file's prior ``sources`` row survives
  unchanged (ADR-0019 §決定 (e) deletion not tracked). The modified
  file's row is upserted (same row id, refreshed fingerprint).

The test deliberately uses :func:`opshub.core.config.OpsHubSettings`'
documented nested-env-var override
(``OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH=<tmp>``) to point the
real :class:`BoxDriveConnector` at a tmp dir, exercising the
production code path — the connector resolves settings, opens the
scanner, walks the dir, and routes each yield through the live
:class:`SourceService`. No stub scanner / no monkeypatched projector —
this is the closeout integration that proves the registered
production connector works end-to-end.

ADR-0019 contracts validated end-to-end:

- ``source_type = "box_drive_file"`` (distinct from Phase 7
  ``"box_event"``; ADR-0019 §決定 (g))
- ``external_id = rel_path`` (POSIX-form, root-relative; ADR-0019
  §決定 (c))
- ``fingerprint = f"{size}:{mtime_ns}"`` persisted on
  ``sources.fingerprint`` (Phase 9 step A2)
- ``actor = "box_drive:local"`` (ADR-0019 §決定 (g))
- Per-file UoW: each yielded file commits a
  :class:`SourceObserved` + :class:`ItemEnqueued` event pair
  atomically (Phase 3 PR #26 contract continues to apply)
- Summary cap ≤ 200 chars (ADR-0005 External Content Minimization,
  enforced by :mod:`opshub.connectors.box_drive.mapper`'s
  ``SUMMARY_MAX_CHARS = 200``)
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# The ``opshub <connector> sync`` CLI imports every registered connector
# package as a side effect (see
# :func:`opshub.connectors._discovery.import_connector_modules` which
# the per-noun driver in :mod:`opshub.cli._connector_common` calls
# before discovery), and the Phase 3 GitHub connector imports ``httpx``
# at module-load time
# through its ``api`` submodule. Skip rather than fail when the
# ``connectors-github`` extras are absent so installations that only
# enabled the box_drive connector still pass the rest of the test
# matrix. Mirrors :mod:`tests.integration.test_phase7_lifecycle`'s
# ``importorskip`` posture for SaaS extras.
pytest.importorskip(
    "httpx",
    reason=(
        "Phase 9 lifecycle drives `opshub connector sync` whose CLI imports the "
        "Phase 3 github connector eagerly; install the 'connectors-github' extras."
    ),
)

from sqlalchemy import text
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite

_PathsDict = dict[str, Path]


# ----------------------------------------------------------------------
# Fixture: re-register the production ``BoxDriveConnector`` after the
# Phase 9 atomicity suite (or any sibling test) has called
# ``unregister_all``. Importing ``opshub.connectors.box_drive`` triggers
# ``register_connector(BoxDriveConnector())`` as a side effect — see
# ``src/opshub/connectors/box_drive/__init__.py``.
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry_to_production() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Ensure the production :class:`BoxDriveConnector` is registered.

    Sibling tests (atomicity suite) install stub connectors and tear
    them down with :func:`unregister_all`. Without this fixture, a test
    that ran after the atomicity suite would observe an empty registry
    and the CLI would refuse with ``unknown connector 'box_drive'``.

    Strategy: only intervene when the registry is missing the
    ``box_drive`` entry. The package's
    :mod:`opshub.connectors.box_drive.__init__` registers a
    :class:`BoxDriveConnector` instance as a documented import side
    effect; if that instance was wiped by an earlier
    :func:`unregister_all` we register a fresh one (the registry's
    "same instance idempotent / different instance under same name
    rejected" rule means a fresh instance is acceptable so long as no
    other ``BoxDriveConnector`` is currently registered, which we
    verified by the membership check).
    """
    from opshub.connectors import discover_connectors, register_connector
    from opshub.connectors.box_drive import BoxDriveConnector

    if "box_drive" not in {c.name for c in discover_connectors()}:
        register_connector(BoxDriveConnector())
    yield
    # Do not call ``unregister_all`` on teardown: it would wipe the
    # GitHub / Slack / MS365 / Box / box_drive registrations the
    # sibling atomicity suite (which uses its own ``unregister_all``
    # + stub install) and any other downstream test relies on. The
    # registry is process-wide so leaving the production state intact
    # is the polite default.


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _row_count(db_path: Path, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>``."""
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
    finally:
        engine.dispose()


def _fetch_sources(db_path: Path) -> list[dict[str, object]]:
    """Return every ``sources`` row whose connector is ``box_drive``."""
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT id, external_id, source_type, title, url, summary, "
                        "fingerprint FROM sources WHERE connector_name = 'box_drive' "
                        "ORDER BY external_id"
                    )
                )
                .mappings()
                .all()
            )
            return [dict(r) for r in rows]
    finally:
        engine.dispose()


def _write_file(path: Path, content: str, *, mtime_ns: int | None = None) -> None:
    """Write ``content`` to ``path``, optionally pinning ``mtime_ns``.

    We pin mtime via :func:`os.utime` so the fingerprint
    ``f"{size}:{mtime_ns}"`` is deterministic across passes — without
    this, Pass 2's "advance mtime" assertion races against the
    filesystem's native nanosecond resolution (some filesystems coarsen
    mtime to microseconds, causing intermittent identical fingerprints
    on fast successive writes).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mtime_ns is not None:
        # ``os.utime`` takes (atime, mtime) as seconds, or (atime_ns,
        # mtime_ns) via ``ns=``. Using ``ns=`` preserves nanosecond
        # precision so the scanner's ``f"{size}:{mtime_ns}"``
        # fingerprint matches what we wrote.
        os.utime(path, ns=(mtime_ns, mtime_ns))


# ----------------------------------------------------------------------
# Lifecycle: 2-pass sync over a tmp ``root_path``
# ----------------------------------------------------------------------


def test_box_drive_lifecycle_2_pass_sync(
    isolated_env: _PathsDict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass 1 observes 3 files, Pass 2 observes only modify + add.

    Walks the ADR-0019 closeout invariants end-to-end through the
    shipped CLI:

    Pass 1 (cold start)
        Three files exist in ``root_path`` (one in a subdir to exercise
        the recursive walk). ``opshub box_drive sync`` is
        invoked. Expected: three new ``sources`` rows under
        ``connector_name='box_drive'`` with
        ``source_type='box_drive_file'`` and populated ``fingerprint``;
        three inbox rows.

    Pass 2 (incremental)
        - ``a.txt`` is modified (content + mtime advanced).
        - ``c.txt`` is deleted.
        - ``d.md`` is added.
        ``opshub box_drive sync`` is invoked. Expected: two
        new :class:`SourceObserved` events (``a.txt`` upserted with a
        refreshed fingerprint + ``d.md`` minted as a new row). The
        ``c.txt`` row remains in ``sources`` (ADR-0019 §決定 (e)
        — deletion is not tracked). Final ``sources`` row count: 3.
    """
    db_path = isolated_env["db_path"]
    drive_root = tmp_path / "box-drive-root"
    drive_root.mkdir()

    # Point the production ``BoxDriveConnector`` at the tmp dir via
    # the documented nested-env-var override. No code change needed —
    # this is the same path operators use in CI / containers.
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__ROOT_PATH", str(drive_root))
    monkeypatch.setenv("OPSHUB_CONNECTORS__BOX_DRIVE__ENABLED", "true")

    # ------------------------------------------------------------------
    # Pre-condition: ``connector list`` surfaces ``box_drive``.
    # ------------------------------------------------------------------
    runner = CliRunner()
    list_result = runner.invoke(app, ["connectors"])
    assert list_result.exit_code == 0, list_result.stdout
    assert "box_drive" in list_result.stdout, (
        "box_drive connector must surface in `opshub connectors` so "
        "operators can discover it even before opting in (ADR-0019 §決定 (a))."
    )

    # ------------------------------------------------------------------
    # Pass 1: seed three files and run a full sync.
    # ------------------------------------------------------------------
    mtime_a_pass1 = 1_700_000_000_000_000_000  # 2023-11-14 UTC, ns since epoch
    mtime_b_pass1 = 1_700_000_001_000_000_000
    mtime_c_pass1 = 1_700_000_002_000_000_000
    _write_file(drive_root / "a.txt", "first version of a", mtime_ns=mtime_a_pass1)
    _write_file(drive_root / "sub" / "b.md", "b body", mtime_ns=mtime_b_pass1)
    _write_file(drive_root / "c.txt", "to be deleted", mtime_ns=mtime_c_pass1)

    pass1 = runner.invoke(app, ["box_drive", "sync"])
    assert pass1.exit_code == 0, pass1.stdout + (pass1.stderr or "")
    assert "synced box_drive: 3 item(s) observed" in pass1.stdout, pass1.stdout
    # Issue #316 post-merge audit: ``opshub connector sync`` drives the
    # indeterminate progress reporter, which is TTY-gated. Under
    # :class:`CliRunner` stderr is a buffer (not a real terminal), so
    # the reporter resolves to the no-op path and no ANSI escapes leak
    # into stdout or stderr. This guard fails fast if a future refactor
    # of the reporter wiring breaks the gate.
    assert "\x1b[" not in pass1.stdout
    assert "\x1b[" not in (pass1.stderr or "")

    # Three rows in ``sources``, three in ``inbox_items``.
    sources_after_pass1 = _fetch_sources(db_path)
    assert len(sources_after_pass1) == 3, sources_after_pass1
    assert _row_count(db_path, "inbox_items") == 3

    # ADR-0019 §決定 (c) — external_id is the POSIX-form rel_path.
    external_ids = sorted(str(row["external_id"]) for row in sources_after_pass1)
    assert external_ids == ["a.txt", "c.txt", "sub/b.md"]

    # ADR-0019 §決定 (g) — source_type is ``box_drive_file``.
    for row in sources_after_pass1:
        assert row["source_type"] == "box_drive_file", row

    # ADR-0005 — summary ≤ 200 chars. With rel_paths this short the
    # summary is the literal ``"path: <rel_path>"`` form; we still
    # check the cap to keep the invariant load-bearing.
    for row in sources_after_pass1:
        summary = row["summary"]
        assert summary is not None and len(str(summary)) <= 200, row

    # ADR-0019 §決定 (d) — ``fingerprint = f"{size}:{mtime_ns}"`` is
    # persisted on the new ``sources.fingerprint`` column.
    by_ext_id_pass1 = {str(row["external_id"]): row for row in sources_after_pass1}
    a_row_pass1 = by_ext_id_pass1["a.txt"]
    expected_a_fp_pass1 = f"{len('first version of a')}:{mtime_a_pass1}"
    assert a_row_pass1["fingerprint"] == expected_a_fp_pass1, a_row_pass1

    # url field carries ``file://<abs_path>`` so operator ``source open``
    # remains workable (Phase 9 plan §4 Open Q #1 resolution).
    assert str(a_row_pass1["url"]).endswith("/box-drive-root/a.txt")
    assert str(a_row_pass1["url"]).startswith("file://")

    # ------------------------------------------------------------------
    # Pass 2: modify ``a.txt``, delete ``c.txt``, add ``d.md``.
    # ------------------------------------------------------------------
    mtime_a_pass2 = mtime_a_pass1 + 60_000_000_000  # +60 seconds, ns
    mtime_d_pass2 = 1_700_000_500_000_000_000
    _write_file(
        drive_root / "a.txt",
        "second version of a (longer content)",
        mtime_ns=mtime_a_pass2,
    )
    (drive_root / "c.txt").unlink()
    _write_file(drive_root / "d.md", "new file", mtime_ns=mtime_d_pass2)
    # Defensive: some filesystems coalesce mtime updates that arrive
    # within the same nanosecond window. Force a small real-time pause
    # so any sub-ns Linux quirks settle (cheap, deterministic).
    time.sleep(0.01)

    pass2 = runner.invoke(app, ["box_drive", "sync"])
    assert pass2.exit_code == 0, pass2.stdout + (pass2.stderr or "")
    # ADR-0019 §決定 (d) — only ``a.txt`` (modified) and ``d.md``
    # (added) yield :class:`SourceObserved`. ``c.txt`` (deleted) is
    # silently ignored per §決定 (e). ``sub/b.md`` is unchanged
    # (matching fingerprint) so the scanner short-circuits it.
    assert "synced box_drive: 2 item(s) observed" in pass2.stdout, pass2.stdout

    sources_after_pass2 = _fetch_sources(db_path)
    by_ext_id_pass2 = {str(row["external_id"]): row for row in sources_after_pass2}

    # Final state:
    # - ``a.txt`` upserted (same ULID, refreshed fingerprint).
    # - ``sub/b.md`` unchanged (skipped by fingerprint match).
    # - ``c.txt`` row remains — ADR-0019 §決定 (e), no deletion event.
    # - ``d.md`` is a fresh row from Pass 2.
    assert sorted(by_ext_id_pass2.keys()) == ["a.txt", "c.txt", "d.md", "sub/b.md"], (
        by_ext_id_pass2.keys()
    )

    # ``a.txt`` was upserted: same ULID (ADR-0010 stable identity on
    # natural key) but a new fingerprint that matches Pass 2's content.
    a_row_pass2 = by_ext_id_pass2["a.txt"]
    assert a_row_pass2["id"] == a_row_pass1["id"], (
        "ADR-0010: re-observation must upsert under the existing "
        "(connector_name, external_id) row, not mint a new ULID."
    )
    expected_a_fp_pass2 = f"{len('second version of a (longer content)')}:{mtime_a_pass2}"
    assert a_row_pass2["fingerprint"] == expected_a_fp_pass2, a_row_pass2
    assert a_row_pass2["fingerprint"] != a_row_pass1["fingerprint"]

    # ``c.txt`` row is unchanged — stale row survives Pass 2 (ADR-0019
    # §決定 (e)). Phase 9.x ``opshub source list --stale`` is the
    # surfacing mechanism.
    c_row_pass2 = by_ext_id_pass2["c.txt"]
    c_row_pass1 = by_ext_id_pass1["c.txt"]
    assert c_row_pass2["fingerprint"] == c_row_pass1["fingerprint"], c_row_pass2
    assert c_row_pass2["id"] == c_row_pass1["id"]

    # ``sub/b.md`` row is unchanged (fingerprint short-circuited the
    # scanner, no SourceObserved event re-appended).
    b_row_pass2 = by_ext_id_pass2["sub/b.md"]
    b_row_pass1 = by_ext_id_pass1["sub/b.md"]
    assert b_row_pass2["fingerprint"] == b_row_pass1["fingerprint"]
    assert b_row_pass2["id"] == b_row_pass1["id"]

    # ``d.md`` is the brand-new row from Pass 2 with a populated fp.
    d_row_pass2 = by_ext_id_pass2["d.md"]
    expected_d_fp = f"{len('new file')}:{mtime_d_pass2}"
    assert d_row_pass2["fingerprint"] == expected_d_fp, d_row_pass2

    # Inbox: 3 (Pass 1) + 2 (Pass 2) = 5 total enqueues.
    assert _row_count(db_path, "inbox_items") == 5
