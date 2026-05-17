"""End-to-end ``opshub workspace ingest`` integration tests (Phase 3 step C3).

Drives the freshly-shipped CLI surface (``opshub workspace ingest`` +
``--dry-run`` flag) through the same ``isolated_env`` fixture the Phase
2 coordination lifecycle tests use. The tests assert only on observable
outputs (exit code, stdout, projection / event row counts) so they pin
the *shipped* CLI contract — never implementation details.

Pinned contracts:

* The normal path emits one :class:`ItemEnqueued` + one
  :class:`FileIngested` event per *new* file (2 events per new file).
* Re-running on unchanged files appends zero events (content-hash
  idempotency, per PR #53 module docstring).
* Modifying a file's bytes changes its SHA-256 and counts as a NEW
  ingest — the dedup is keyed on content, not file path. This is the
  documented C2 contract; see the test comment for the rationale.
* ``--dry-run`` reads the projection but never appends events or updates
  it (the event log and the ``ingested_files`` projection both stay at
  their pre-call baselines).
* ``workspace/inbox/`` missing is a no-op, not an error.
* Non-``.md`` siblings are silently ignored (``glob("*.md")`` filter).
* Sub-directories under ``inbox/`` are NOT recursed into (immediate
  children only — the C2 service contract).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Table, select, text
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.inbox import inbox_items_table
from opshub.projections.ingested_files import ingested_files_table

_PathsDict = dict[str, Path]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Run ``opshub`` via CliRunner and return ``(exit_code, stdout, stderr)``."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


def _row_count(db_path: Path, table_name: str) -> int:
    """Return ``SELECT COUNT(*)`` for ``table_name`` on a short-lived engine."""
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
    finally:
        engine.dispose()


def _all_rows(db_path: Path, table: Table) -> list[dict[str, Any]]:
    """Return every row of ``table`` as plain dicts."""
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            result = conn.execute(select(table)).mappings().all()
        return [dict(row) for row in result]
    finally:
        engine.dispose()


def _write_inbox_file(workspace_root: Path, name: str, body: str) -> Path:
    """Write ``body`` to ``<workspace_root>/inbox/<name>`` and return the path.

    Creates the ``inbox`` parent directory on demand. ``body`` is
    written verbatim (no implicit trailing newline) so per-test
    fixtures stay byte-exact.
    """
    inbox_dir = workspace_root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def _events_baseline(db_path: Path) -> int:
    """Return the post-``init`` event count for the freshly provisioned DB.

    ``isolated_env`` runs ``opshub init`` which provisions the schema
    but does not append any domain events (the schema-bootstrap path is
    DDL only). Asserting against this baseline makes the test resilient
    to any future "init seeds N events" change.
    """
    return _row_count(db_path, "events")


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_ingest_enqueues_new_inbox_files(isolated_env: _PathsDict) -> None:
    """Three varied ``.md`` files → three ItemEnqueued + three FileIngested.

    The three files cover the three resolution paths in
    :func:`opshub.markdown.ingest.parse_inbox_file`:

    1. Full front-matter (``summary:`` + ``source_ref:``).
    2. Front-matter with ``summary:`` only.
    3. No front-matter at all → falls back to the filename stem (with
       hyphens rewritten to spaces).
    """
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline = _events_baseline(db_path)

    _write_inbox_file(
        workspace_root,
        "alpha.md",
        "---\nsummary: review pr 99\nsource_ref: github:owner/repo#99\n---\nbody body\n",
    )
    _write_inbox_file(
        workspace_root,
        "beta.md",
        "---\nsummary: just a summary\n---\n",
    )
    _write_inbox_file(
        workspace_root,
        "no-frontmatter-note.md",
        "free-form note without front matter\n",
    )

    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 3 item(s), skipped 0" in out, out

    # Six events on top of the post-init baseline (3 ItemEnqueued + 3
    # FileIngested), per the C2 contract.
    assert _row_count(db_path, "events") == baseline + 6

    # Three pending inbox rows with the expected summaries.
    inbox_rows = _all_rows(db_path, inbox_items_table)
    by_summary = {row["summary"]: row for row in inbox_rows}
    assert "review pr 99" in by_summary
    assert by_summary["review pr 99"]["state"] == "pending"
    assert by_summary["review pr 99"]["source_ref"] == "github:owner/repo#99"
    assert "just a summary" in by_summary
    assert by_summary["just a summary"]["state"] == "pending"
    # The no-front-matter path falls back to the filename stem with
    # hyphens rewritten to spaces (see ``_summary_from_filename``).
    assert "no frontmatter note" in by_summary
    assert by_summary["no frontmatter note"]["state"] == "pending"

    # Three ingested_files rows, one per content hash.
    ingested_rows = _all_rows(db_path, ingested_files_table)
    assert len(ingested_rows) == 3
    # Every row carries a 64-char SHA-256 hex digest.
    for row in ingested_rows:
        assert isinstance(row["content_hash"], str)
        assert len(row["content_hash"]) == 64


def test_ingest_is_idempotent(isolated_env: _PathsDict) -> None:
    """Second run on unchanged files appends zero events, reports 3 skipped."""
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline = _events_baseline(db_path)

    for name in ("a.md", "b.md", "c.md"):
        _write_inbox_file(workspace_root, name, f"---\nsummary: {name} summary\n---\n")

    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 3 item(s), skipped 0" in out, out
    assert _row_count(db_path, "events") == baseline + 6

    # Second run: hashes unchanged, so every file is skipped.
    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 0 item(s), skipped 3" in out, out
    # The event count must NOT have grown — content-hash dedup is the
    # whole point of the ``ingested_files`` projection (PR #53).
    assert _row_count(db_path, "events") == baseline + 6
    # And the inbox projection still has exactly three rows.
    assert _row_count(db_path, "inbox_items") == 3


def test_ingest_picks_up_changed_files_as_new(isolated_env: _PathsDict) -> None:
    """Editing a file's bytes mints a new content hash → new ingest.

    Per the documented C2 contract (PR #50 / PR #53 module docstrings),
    the dedup key is the file's SHA-256 content hash, **not** its path.
    A whitespace edit still re-ingests; the user changed the file, the
    workspace ingest path treats that as a new item. This test pins the
    behaviour so a future reviewer reading the projection state does not
    misread "two rows for one path" as a bug.
    """
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline = _events_baseline(db_path)

    note = _write_inbox_file(
        workspace_root,
        "note.md",
        "---\nsummary: original\n---\n",
    )

    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 1 item(s), skipped 0" in out, out
    assert _row_count(db_path, "events") == baseline + 2  # 1 ItemEnqueued + 1 FileIngested

    # Modify the file's body — different bytes, different SHA-256.
    note.write_text("---\nsummary: revised\n---\nappended line\n", encoding="utf-8")

    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    # The new content is unknown, so it counts as a fresh enqueue. The
    # original hash is no longer present in the directory but stays in
    # the projection (the projection is keyed by content_hash, not
    # path), so skipped == 0 here.
    assert "enqueued 1 item(s), skipped 0" in out, out
    assert _row_count(db_path, "events") == baseline + 4
    # Two inbox rows: one per (different) content hash.
    assert _row_count(db_path, "inbox_items") == 2
    # And the ingested_files projection now has two distinct hashes.
    ingested_rows = _all_rows(db_path, ingested_files_table)
    assert len({row["content_hash"] for row in ingested_rows}) == 2


def test_dry_run_does_not_write_events(isolated_env: _PathsDict) -> None:
    """``--dry-run`` reports candidates without appending events."""
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline = _events_baseline(db_path)

    _write_inbox_file(workspace_root, "x.md", "---\nsummary: x\n---\n")
    _write_inbox_file(workspace_root, "y.md", "---\nsummary: y\n---\n")

    code, out, _ = _invoke(["workspace", "ingest", "--dry-run"])
    assert code == 0, out
    assert "would enqueue 2 file(s), skip 0" in out, out
    # Both file paths appear under the ``+`` (would enqueue) prefix.
    assert "+ inbox/x.md" in out, out
    assert "+ inbox/y.md" in out, out

    # No events appended — the dry-run path never constructs the
    # FileIngestService, so there is no writer to mutate state.
    assert _row_count(db_path, "events") == baseline
    # And the ``ingested_files`` projection is still empty.
    assert _row_count(db_path, "ingested_files") == 0
    # The ``inbox_items`` projection is also untouched.
    assert _row_count(db_path, "inbox_items") == 0


def test_ingest_with_no_inbox_dir(isolated_env: _PathsDict) -> None:
    """No ``inbox/`` directory → 0 enqueued, 0 skipped, exit 0."""
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline = _events_baseline(db_path)

    # ``isolated_env`` provisions ``<workspace_root>`` itself; ensure
    # the ``inbox`` child does NOT exist.
    inbox_dir = workspace_root / "inbox"
    assert not inbox_dir.exists()

    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 0 item(s), skipped 0" in out, out
    assert _row_count(db_path, "events") == baseline


def test_ingest_ignores_non_markdown_files(isolated_env: _PathsDict) -> None:
    """Only ``*.md`` is considered — siblings with other extensions are skipped."""
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline = _events_baseline(db_path)

    _write_inbox_file(workspace_root, "note.md", "---\nsummary: real note\n---\n")
    _write_inbox_file(workspace_root, "note.txt", "plain text, must be ignored\n")
    _write_inbox_file(workspace_root, "script.sh", "#!/bin/sh\necho hi\n")

    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    assert "enqueued 1 item(s), skipped 0" in out, out
    assert _row_count(db_path, "events") == baseline + 2  # exactly one new file
    inbox_rows = _all_rows(db_path, inbox_items_table)
    assert len(inbox_rows) == 1
    assert inbox_rows[0]["summary"] == "real note"


def test_ingest_handles_subdirectory(isolated_env: _PathsDict) -> None:
    """Sub-directories under ``inbox/`` are not recursed into (C2 contract)."""
    workspace_root = isolated_env["workspace_root"]
    db_path = isolated_env["db_path"]
    baseline = _events_baseline(db_path)

    inbox_dir = workspace_root / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "foo.md").write_text("---\nsummary: foo top-level\n---\n", encoding="utf-8")

    subdir = inbox_dir / "subdir"
    subdir.mkdir(parents=True, exist_ok=True)
    (subdir / "bar.md").write_text("---\nsummary: bar in subdir\n---\n", encoding="utf-8")

    code, out, _ = _invoke(["workspace", "ingest"])
    assert code == 0, out
    # Only ``foo.md`` is in scope (``glob("*.md")`` not ``rglob``).
    assert "enqueued 1 item(s), skipped 0" in out, out
    assert _row_count(db_path, "events") == baseline + 2
    inbox_rows = _all_rows(db_path, inbox_items_table)
    assert len(inbox_rows) == 1
    assert inbox_rows[0]["summary"] == "foo top-level"
