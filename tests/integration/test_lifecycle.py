"""End-to-end Phase 1 lifecycle test driven entirely through the CLI.

This is the Phase 1 DoD tripwire: it exercises every shipped command in
the order a fresh user would touch them, asserting only on observable
outputs (exit codes, stdout, on-disk state). No internal modules are
imported beyond the Typer app entry point — that constraint is the
whole point of the lifecycle test, since it pins down the *shipped*
contract rather than the implementation details.

The sequence covered:

1. ``opshub init`` — creates config/data/workspace dirs + runs migrations.
2. ``opshub task create`` (twice) — appends events and projects rows.
3. ``opshub task list --format json`` — reads the projection.
4. ``opshub projections rebuild`` — disposable read model contract
   (ADR-0002): replay must reproduce the same row count.
5. ``opshub workspace generate`` (twice) — disposable workspace
   contract (ADR-0003): first call writes ``index.md`` + per-task files,
   second call is a no-op.
6. ``opshub embeddings status`` — Phase 4 step B3 implementation: with
   the default ``backend=disabled`` it prints a one-line hint and exits
   0 without scanning the DB.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.markdown.tasks import INDEX_FILENAME
from opshub.projections.tasks import tasks_table


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point every OpsHub path env var inside ``tmp_path``.

    Mirrors the helper used in the per-command unit tests so this
    lifecycle test stays insulated from the developer's real
    ``~/.config/opshub`` / ``~/.local/share/opshub`` directories.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = data_dir / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))

    return {
        "config_dir": config_dir,
        "data_dir": data_dir,
        "workspace_root": workspace_root,
        "db_path": db_path,
    }


def _events_row_count(db_path: Path) -> int:
    """Return the number of rows in the ``events`` table.

    Uses a raw ``SELECT COUNT(*)`` to avoid coupling this lifecycle test
    to the in-process SQLAlchemy ``Table`` registration (which only
    happens once :class:`SqlAlchemyEventStore` is instantiated). The CLI
    contract is the schema on disk, not the ORM mapping.
    """
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM events")).scalar_one()
    finally:
        engine.dispose()
    return int(count)


def _tasks_projection_rows(db_path: Path) -> list[dict[str, Any]]:
    """Return every row from the ``tasks`` projection as plain dicts."""
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(tasks_table)).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


def test_full_phase1_lifecycle_via_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Drive every shipped Phase 1 command in the canonical user order."""
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    # ---- 1. opshub init ----------------------------------------------------
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    # Config file lives at <config_dir>/config.toml.
    config_path = paths["config_dir"] / "config.toml"
    assert config_path.is_file(), "opshub init must write config.toml"

    # The DB has the events table now (proxy for "schema migrated").
    assert paths["db_path"].is_file(), "opshub init must create the SQLite DB"
    assert _events_row_count(paths["db_path"]) == 0

    # ---- 2. opshub task create (x2) ----------------------------------------
    create_first = runner.invoke(app, ["task", "create", "first task"])
    assert create_first.exit_code == 0, create_first.stdout
    first_id = create_first.stdout.strip()
    # Output contract: exactly one 26-character ULID on stdout.
    assert len(first_id) == 26, first_id

    # Events table now has 1 row, projection has 1 draft row.
    assert _events_row_count(paths["db_path"]) == 1
    rows_after_first = _tasks_projection_rows(paths["db_path"])
    assert len(rows_after_first) == 1
    assert rows_after_first[0]["state"] == "draft"
    assert rows_after_first[0]["id"] == first_id

    create_second = runner.invoke(app, ["task", "create", "second task", "--body", "with body"])
    assert create_second.exit_code == 0, create_second.stdout
    second_id = create_second.stdout.strip()
    assert len(second_id) == 26, second_id
    assert second_id != first_id

    # The second create added exactly one row to events + projection.
    assert _events_row_count(paths["db_path"]) == 2
    rows_after_second = _tasks_projection_rows(paths["db_path"])
    assert len(rows_after_second) == 2
    by_id = {row["id"]: row for row in rows_after_second}
    assert by_id[second_id]["body"] == "with body"
    assert by_id[second_id]["state"] == "draft"

    # ---- 3. opshub task list --format json ---------------------------------
    list_result = runner.invoke(app, ["task", "list", "--format", "json"])
    assert list_result.exit_code == 0, list_result.stdout

    payload: list[dict[str, Any]] = json.loads(list_result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 2
    listed_ids = {row["id"] for row in payload}
    assert listed_ids == {first_id, second_id}
    # Documented JSON keys per ADR-0002 / step 14: id, title, state, updated_at.
    for row in payload:
        for key in ("id", "title", "state", "updated_at"):
            assert key in row, f"missing key {key!r} in task list JSON row"

    # ---- 4. opshub projections rebuild -------------------------------------
    rebuild_result = runner.invoke(app, ["projections", "rebuild"])
    assert rebuild_result.exit_code == 0, rebuild_result.stdout
    # Report contract from cli/projections.py: "rebuilt N projection(s) from M event(s)".
    # Projection count grows as Phase 2 adds new projections; assert format presence only.
    assert "2 event(s)" in rebuild_result.stdout
    assert "projection(s)" in rebuild_result.stdout

    # Replay preserves the row count.
    rows_after_rebuild = _tasks_projection_rows(paths["db_path"])
    assert len(rows_after_rebuild) == 2
    assert {row["id"] for row in rows_after_rebuild} == {first_id, second_id}

    # ---- 5. opshub workspace generate --------------------------------------
    generate_result = runner.invoke(app, ["workspace", "generate"])
    assert generate_result.exit_code == 0, generate_result.stdout
    # 6 files = tasks index + 2 per-task .md + 3 empty indexes (inbox /
    # decisions / handoffs). Phase 2 step 8 adds three renderers, each
    # of which always emits its own ``index.md`` even when its
    # projection is empty.
    assert "wrote 6 file(s)" in generate_result.stdout

    generated_dir = paths["workspace_root"] / "generated" / "tasks"
    assert (generated_dir / INDEX_FILENAME).is_file()
    assert (generated_dir / f"{first_id}.md").is_file()
    assert (generated_dir / f"{second_id}.md").is_file()

    # Second invocation is idempotent: no files written.
    generate_again = runner.invoke(app, ["workspace", "generate"])
    assert generate_again.exit_code == 0, generate_again.stdout
    assert "wrote 0 file(s)" in generate_again.stdout

    # ---- 6. opshub embeddings status ---------------------------------------
    status_result = runner.invoke(app, ["embeddings", "status"])
    assert status_result.exit_code == 0, status_result.stdout
    # Phase 4 step B3: disabled backend short-circuits with a hint
    # (no DB scan, no row count). The active-backend path is covered by
    # tests/unit/cli/test_embeddings.py.
    assert "backend=disabled" in status_result.stdout
    assert "embeddings rebuild" in status_result.stdout
