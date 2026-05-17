"""Phase 3 end-to-end lifecycle tests.

Each test function drives one Phase 3 cross-workstream flow through the
shipped CLI and asserts on observable outputs (exit codes, stdout,
projection rows, workspace files). Internal modules are imported only
to inspect on-disk state, never to drive the workflow.

The split mirrors the Phase 2 closeout (``test_coordination_lifecycle``)
shape: one test function per workstream rather than a single monolithic
lifecycle. Per-workstream tests give pytest's selector enough granularity
to re-run a single Phase 3 flow when an investigation needs to, and they
keep each function under ~80 LOC.

``isolated_env`` fixture (``tests/integration/conftest.py``) provisions
``OPSHUB_*`` env, runs ``init``, and yields a paths dict. Each test gets
a fresh ``tmp_path`` — no inter-test state sharing.

GitHub connector tests monkeypatch ``opshub.connectors.github.api`` so
the suite never reaches the network. The mocking shape mirrors
:mod:`tests.integration.test_github_connector_lifecycle` from PR #55;
the D1 variant drives the connector through the **CLI**
(``opshub connector sync github``) rather than the service directly, so
the full sync-bracket + cursor-persistence + summary-stdout contract is
exercised end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.inbox import inbox_items_table
from opshub.projections.sources import sources_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opshub.connectors.github.api import GitHubItem

_PathsDict = dict[str, Path]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Run the OpsHub Typer app and return ``(exit_code, stdout, stderr)``."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


def _row_count(engine: Engine, table_name: str) -> int:
    """Return ``SELECT COUNT(*)`` for ``table_name``."""
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())


def _events_baseline(db_path: Path) -> int:
    """Return the post-``init`` event count for the freshly provisioned DB.

    ``isolated_env`` runs ``opshub init`` which provisions the schema but
    does not append any domain events. Asserting against this baseline
    keeps the test resilient to any future "init seeds N events" change
    and lets us measure the *delta* a Phase 3 flow contributes.
    """
    engine = create_engine_for_sqlite(db_path)
    try:
        return _row_count(engine, "events")
    finally:
        engine.dispose()


def _make_github_item(
    source_type: str,
    external_id: str,
    *,
    updated_at: datetime,
) -> GitHubItem:
    """Build a :class:`GitHubItem` payload for the mocked API helpers.

    Kept as a free helper rather than a fixture: the per-test mocking
    shape (which items each list helper returns) varies, so a fixture
    would over-bind. The lazy import matches the pattern used by
    :mod:`test_github_connector_lifecycle` — the module deliberately
    avoids a top-level connector import so the test module stays cheap to
    collect on test selectors that never touch the GitHub paths.
    """
    from opshub.connectors.github.api import GitHubItem

    return GitHubItem(
        source_type=source_type,
        external_id=external_id,
        title=f"{source_type} {external_id}",
        url=f"https://example.invalid/{external_id}",
        summary=f"summary of {external_id}",
        updated_at=updated_at,
    )


def _patch_github_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    issues: list[GitHubItem],
    pulls: list[GitHubItem],
    notifications: list[GitHubItem],
) -> None:
    """Replace the three ``opshub.connectors.github.api`` list_* helpers.

    The connector imports the module-bound name (``github_api``) rather
    than the individual symbols, so monkeypatching the attributes on the
    module object is the cheapest possible test seam — no fake httpx
    transport is needed for the E2E test (the fetch primitives have their
    own coverage in :mod:`tests.unit.connectors.github.test_api`).
    """
    from opshub.connectors.github import api as github_api

    def _fake_issues(*_args: object, **_kwargs: object) -> Iterator[GitHubItem]:
        return iter(issues)

    def _fake_pulls(*_args: object, **_kwargs: object) -> Iterator[GitHubItem]:
        return iter(pulls)

    def _fake_notifications(*_args: object, **_kwargs: object) -> Iterator[GitHubItem]:
        return iter(notifications)

    monkeypatch.setattr(github_api, "list_issues_since", _fake_issues)
    monkeypatch.setattr(github_api, "list_pulls_since", _fake_pulls)
    monkeypatch.setattr(github_api, "list_notifications", _fake_notifications)


def _write_inbox_file(workspace_root: Path, name: str, body: str) -> Path:
    """Write ``body`` to ``<workspace_root>/inbox/<name>`` and return the path."""
    inbox_dir = workspace_root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / name
    path.write_text(body, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Sub-issue B: GitHub connector → inbox via CLI
# ----------------------------------------------------------------------


def test_github_connector_to_inbox_e2e(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``opshub connector sync github`` → 4 items observed → inbox triage.

    Drives the full sub-issue B surface through the shipped CLI:

    1. Set the two env vars the connector expects
       (``OPSHUB_CONNECTOR_GITHUB_REPO`` / ``OPSHUB_CONNECTOR_GITHUB_PAT``)
       and mock the three GitHub fetch primitives to return 2 issues +
       1 PR + 1 notification — exactly the documented Phase 3 MVP scope.
    2. Invoke ``opshub connector sync github`` and assert the documented
       one-line summary (``"synced github: 4 item(s) observed"``).
    3. Inspect the event log + projections:
       * 10 new events on top of the post-init baseline:
         ``ConnectorSyncStarted`` + 4 x (``SourceObserved`` +
         ``ItemEnqueued``) + ``ConnectorSyncCompleted``.
       * ``sources`` projection has 4 rows, all with
         ``connector_name='github'``.
       * ``inbox_items`` has 4 ``pending`` rows.
    4. Triage one of the inbox items via ``opshub inbox triage <id>
       --to-task ...``; assert the corresponding ``tasks`` row appears
       and the inbox row flips to ``state='triaged_to_task'``.
    """
    db_path = isolated_env["db_path"]
    baseline_events = _events_baseline(db_path)

    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_test")

    times = [
        datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
    ]
    issues = [
        _make_github_item("issue", "owner/repo#1", updated_at=times[0]),
        _make_github_item("issue", "owner/repo#2", updated_at=times[1]),
    ]
    pulls = [_make_github_item("pull_request", "owner/repo#10", updated_at=times[2])]
    notifications = [_make_github_item("notification", "n1", updated_at=times[3])]
    _patch_github_api(monkeypatch, issues=issues, pulls=pulls, notifications=notifications)

    # ---- 2. CLI sync -----------------------------------------------------
    code, out, _ = _invoke(["connector", "sync", "github"])
    assert code == 0, out
    # The summary line is the documented PR #48 shape; pinning the exact
    # wording catches accidental refactors of ``cli/connector.py``.
    assert "synced github: 4 item(s) observed" in out, out

    # ---- 3. on-disk state ------------------------------------------------
    engine = create_engine_for_sqlite(db_path)
    try:
        # 1 ConnectorSyncStarted + 4 SourceObserved + 4 ItemEnqueued +
        # 1 ConnectorSyncCompleted = 10 new events.
        assert _row_count(engine, "events") == baseline_events + 10
        # Sources projection: 4 distinct external IDs → 4 rows.
        source_rows = list(engine.connect().execute(select(sources_table)).mappings().all())
        assert len(source_rows) == 4
        assert {row["connector_name"] for row in source_rows} == {"github"}
        # Inbox: 4 pending rows, all keyed back to the connector.
        inbox_rows = list(engine.connect().execute(select(inbox_items_table)).mappings().all())
        assert len(inbox_rows) == 4
        assert all(row["state"] == "pending" for row in inbox_rows)
        assert {row["source_ref"] for row in inbox_rows} == {
            "github:owner/repo#1",
            "github:owner/repo#2",
            "github:owner/repo#10",
            "github:n1",
        }
    finally:
        engine.dispose()

    # ---- 4. triage one item → task --------------------------------------
    # Pick any inbox row; the triage path is identical regardless of which
    # of the four observations we pick.
    engine = create_engine_for_sqlite(db_path)
    try:
        first_inbox_id = next(
            iter(engine.connect().execute(select(inbox_items_table.c.id)).scalars())
        )
    finally:
        engine.dispose()

    code, out, _ = _invoke(["inbox", "triage", first_inbox_id, "--to-task", "review issue"])
    assert code == 0, out
    new_task_id = out.strip()
    assert len(new_task_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "tasks") == 1
        inbox_rows_by_id = {
            row["id"]: row
            for row in engine.connect().execute(select(inbox_items_table)).mappings().all()
        }
        assert inbox_rows_by_id[first_inbox_id]["state"] == "triaged_to_task"
        assert inbox_rows_by_id[first_inbox_id]["target_id"] == new_task_id
    finally:
        engine.dispose()


# ----------------------------------------------------------------------
# Sub-issue C: workspace inbox file ingest via CLI + idempotency
# ----------------------------------------------------------------------


def test_workspace_inbox_file_ingest_e2e(isolated_env: _PathsDict) -> None:
    """``opshub workspace ingest`` → 3 files → triage → re-run idempotent.

    Mirrors :mod:`test_workspace_ingest_lifecycle` (PR #54) for the
    three-files happy path but extends it with a triage step and a second
    ingest run to prove the C2/C3 contracts compose:

    1. Write three ``.md`` files under ``<workspace>/inbox/`` covering the
       three front-matter resolution paths (full / summary-only / none).
    2. Run ``opshub workspace ingest``; assert the documented summary
       (``"enqueued 3 item(s), skipped 0"``) and that 6 events
       (3 ``ItemEnqueued`` + 3 ``FileIngested``) appended above baseline.
    3. Triage one item via ``opshub inbox triage <id> --to-task ...`` and
       confirm the ``tasks`` projection picks up the new row.
    4. Re-run ``opshub workspace ingest`` and assert ``"enqueued 0
       item(s), skipped 3"`` — content-hash idempotency is the whole
       point of the ``ingested_files`` projection (PR #53).
    """
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline_events = _events_baseline(db_path)

    _write_inbox_file(
        workspace_root,
        "alpha.md",
        "---\nsummary: review pr 99\nsource_ref: github:owner/repo#99\n---\nbody\n",
    )
    _write_inbox_file(
        workspace_root,
        "beta.md",
        "---\nsummary: summary only\n---\n",
    )
    _write_inbox_file(
        workspace_root,
        "no-frontmatter-note.md",
        "free-form note\n",
    )

    # ---- 2. first ingest -------------------------------------------------
    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 3 item(s), skipped 0" in out, out

    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "events") == baseline_events + 6
        assert _row_count(engine, "inbox_items") == 3
        assert _row_count(engine, "ingested_files") == 3
        # Pull one inbox id for the triage step that follows.
        first_inbox_id = next(
            iter(engine.connect().execute(select(inbox_items_table.c.id)).scalars())
        )
    finally:
        engine.dispose()

    # ---- 3. triage one of them ------------------------------------------
    code, out, _ = _invoke(["inbox", "triage", first_inbox_id, "--to-task", "follow up"])
    assert code == 0, out
    new_task_id = out.strip()
    assert len(new_task_id) == 26

    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "tasks") == 1
    finally:
        engine.dispose()

    # ---- 4. re-run ingest is a no-op ------------------------------------
    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 0 item(s), skipped 3" in out, out

    engine = create_engine_for_sqlite(db_path)
    try:
        # Inbox + ingested_files row counts unchanged; the second ingest
        # must not append events either (besides the triage's
        # ItemTriaged + TaskCreated from step 3).
        assert _row_count(engine, "inbox_items") == 3
        assert _row_count(engine, "ingested_files") == 3
        # 6 from first ingest + 2 from the triage step. The second ingest
        # appended zero — content-hash dedup wins.
        assert _row_count(engine, "events") == baseline_events + 6 + 2
    finally:
        engine.dispose()


# ----------------------------------------------------------------------
# Cross-workstream: workspace generate covers Phase 3 entities idempotently
# ----------------------------------------------------------------------


def test_phase3_workspace_generate_includes_phase3_entities_idempotent(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed via both Phase 3 entrance paths; ``workspace generate`` is idempotent.

    The Phase 2 closeout test pins the workspace-generate idempotency
    contract for Phase 1+2 entities (ADR-0002 + ADR-0003: a second run on
    unchanged state must write zero files). Phase 3 extends the
    projection set with ``sources`` / ``connector_cursors`` /
    ``ingested_files`` and adds two new entrance paths (connector sync +
    workspace ingest). This test proves the contract still holds after
    Phase 3 entities are mixed in.

    Steps:

    1. Seed via the GitHub connector path (mocked httpx) — adds rows in
       ``sources`` / ``inbox_items`` / ``connector_cursors``.
    2. Seed via the workspace file ingest path — adds rows in
       ``ingested_files`` + more ``inbox_items``.
    3. Run ``opshub workspace generate`` twice; second run must report
       ``"wrote 0 file(s)"`` (the Phase 2-pinned idempotency wording).
    """
    workspace_root = isolated_env["workspace_root"]

    # ---- 1. seed via connector sync (mocked) ----------------------------
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_test")
    t = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    _patch_github_api(
        monkeypatch,
        issues=[_make_github_item("issue", "owner/repo#1", updated_at=t)],
        pulls=[_make_github_item("pull_request", "owner/repo#10", updated_at=t)],
        notifications=[],
    )
    code, out, _ = _invoke(["connector", "sync", "github"])
    assert code == 0, out
    assert "synced github: 2 item(s) observed" in out, out

    # ---- 2. seed via workspace inbox file ingest -------------------------
    _write_inbox_file(
        workspace_root,
        "note.md",
        "---\nsummary: phase 3 note\n---\nbody\n",
    )
    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 1 item(s), skipped 0" in out, out

    # ---- 3. workspace generate twice ------------------------------------
    code, first_out, _ = _invoke(["workspace", "generate"])
    assert code == 0, first_out
    # First run must write at least one file (we don't pin a specific
    # count: per-renderer index counts are a Phase 2 step 8 implementation
    # detail covered by ``test_coordination_lifecycle``).
    assert "wrote" in first_out
    assert "wrote 0 file(s)" not in first_out, first_out

    code, second_out, _ = _invoke(["workspace", "generate"])
    assert code == 0, second_out
    assert "wrote 0 file(s)" in second_out, second_out

    # Phase 3 entity projections must survive the regenerate cycle — they
    # are read by the renderer registry but not (currently) materialised
    # as their own ``generated/*`` subtree; the asserts here pin the
    # invariant that the Phase 2 renderer set still renders unchanged.
    db_path = isolated_env["db_path"]
    engine = create_engine_for_sqlite(db_path)
    try:
        assert _row_count(engine, "sources") == 2
        assert _row_count(engine, "connector_cursors") == 1
        assert _row_count(engine, "ingested_files") == 1
    finally:
        engine.dispose()


# Re-export ``pytest`` so static analysers see this module is a pytest test
# (the import would otherwise read as unused).
_ = pytest
