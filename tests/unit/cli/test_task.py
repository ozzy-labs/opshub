"""Tests for ``opshub task`` (create / list).

Every test isolates the CLI invocation via ``monkeypatch.setenv`` so the
user's real ``~/.config/opshub`` / ``~/.local/share/opshub`` directories are
never touched. ``opshub init`` is invoked through the CLI to provision the
schema before each command exercise — that mirrors how a real user reaches
``opshub task ...``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.ids import parse_ulid_timestamp_ms
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.tasks import tasks_table


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every OpsHub path env var inside ``tmp_path`` and return the DB path."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))

    return db_path


def _run_init(runner: CliRunner) -> None:
    """Run ``opshub init`` and assert it succeeded."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout


# ---- task create ----------------------------------------------------------


def test_task_create_prints_ulid_and_persists_event_and_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["task", "create", "hello"])
    assert result.exit_code == 0, result.stdout

    task_id = result.stdout.strip()
    # Output is exactly one 26-character ULID.
    assert len(task_id) == 26
    # And it round-trips through the ULID parser.
    parse_ulid_timestamp_ms(task_id)

    # The events table has one row, the tasks projection has one draft row.
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            events_table = tasks_table.metadata.tables["events"]
            events = conn.execute(
                select(events_table.c.event_type, events_table.c.aggregate_id)
            ).all()
            tasks = conn.execute(select(tasks_table)).mappings().all()
    finally:
        engine.dispose()

    assert len(events) == 1
    assert events[0].event_type == "task.created"
    assert events[0].aggregate_id == task_id

    assert len(tasks) == 1
    row = tasks[0]
    assert row["id"] == task_id
    assert row["title"] == "hello"
    assert row["state"] == "draft"


def test_task_create_with_body_and_actor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(
        app,
        [
            "task",
            "create",
            "with body",
            "--body",
            "longer note",
            "--actor",
            "agent:planner",
        ],
    )
    assert result.exit_code == 0, result.stdout
    task_id = result.stdout.strip()

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            events_table = tasks_table.metadata.tables["events"]
            rows = conn.execute(select(events_table.c.actor, events_table.c.aggregate_id)).all()
            task_row = (
                conn.execute(select(tasks_table).where(tasks_table.c.id == task_id))
                .mappings()
                .one()
            )
    finally:
        engine.dispose()

    assert len(rows) == 1
    assert rows[0].actor == "agent:planner"
    assert task_row["body"] == "longer note"


def test_task_create_rejects_empty_title(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["task", "create", ""])
    assert result.exit_code != 0


# ---- task list ------------------------------------------------------------


def test_task_list_json_returns_array_of_created_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    first = runner.invoke(app, ["task", "create", "first"])
    assert first.exit_code == 0, first.stdout
    second = runner.invoke(app, ["task", "create", "second"])
    assert second.exit_code == 0, second.stdout

    result = runner.invoke(app, ["task", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout

    payload: list[dict[str, Any]] = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 2
    titles = {row["title"] for row in payload}
    assert titles == {"first", "second"}
    # Every row exposes the canonical columns.
    for row in payload:
        for key in ("id", "title", "state", "created_at", "updated_at"):
            assert key in row


def test_task_list_md_contains_markdown_table_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    create_result = runner.invoke(app, ["task", "create", "md test"])
    assert create_result.exit_code == 0, create_result.stdout

    result = runner.invoke(app, ["task", "list", "--format", "md"])
    assert result.exit_code == 0, result.stdout

    # Markdown table headers + separator row.
    assert "| ID |" in result.stdout
    assert "| State |" in result.stdout
    assert "| --- |" in result.stdout
    # The created task title shows up.
    assert "md test" in result.stdout


def test_task_list_table_default_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    create_result = runner.invoke(app, ["task", "create", "table test"])
    assert create_result.exit_code == 0, create_result.stdout

    result = runner.invoke(app, ["task", "list"])
    assert result.exit_code == 0, result.stdout
    # Aligned-column header.
    assert "ID" in result.stdout
    assert "STATE" in result.stdout
    assert "TITLE" in result.stdout
    assert "UPDATED" in result.stdout
    assert "table test" in result.stdout


def test_task_list_invalid_format_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["task", "list", "--format", "invalid"])
    assert result.exit_code != 0
    # The ValidationError mentions the bad value so users can self-correct.
    assert result.exception is not None
    assert "invalid" in str(result.exception).lower()


def test_task_list_state_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    runner.invoke(app, ["task", "create", "draft 1"])
    runner.invoke(app, ["task", "create", "draft 2"])

    result = runner.invoke(app, ["task", "list", "--format", "json", "--state", "draft"])
    assert result.exit_code == 0, result.stdout
    payload: list[dict[str, Any]] = json.loads(result.stdout)
    assert all(row["state"] == "draft" for row in payload)
    assert len(payload) == 2

    # Filtering on a state with no rows returns an empty array.
    completed = runner.invoke(app, ["task", "list", "--format", "json", "--state", "completed"])
    assert completed.exit_code == 0, completed.stdout
    assert json.loads(completed.stdout) == []


def test_task_list_against_uninitialised_db_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running ``task list`` before ``opshub init`` surfaces a ConfigError.

    The wiring helper inspects the schema and refuses to render against a DB
    that has no ``events`` table yet. The error message must point the user
    at ``opshub init`` so they can recover without reading source code.
    """
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    # Note: NO opshub init call here.
    result = runner.invoke(app, ["task", "list"])
    assert result.exit_code != 0
    # Inspect the raised exception for the actionable hint.
    assert result.exception is not None
    assert "opshub init" in str(result.exception)
