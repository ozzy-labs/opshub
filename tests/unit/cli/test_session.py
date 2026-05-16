"""Tests for ``opshub session`` (start / end / list).

Every test isolates the CLI invocation via ``monkeypatch.setenv`` so the
user's real ``~/.config/opshub`` / ``~/.local/share/opshub`` /
``~/.local/state/opshub`` directories are never touched. ``opshub init``
is invoked through the CLI to provision the schema before each command
exercise — that mirrors how a real user reaches ``opshub session ...``.
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
from opshub.projections.work_sessions import work_sessions_table


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point every OpsHub path env var inside ``tmp_path`` and return relevant paths."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    state_home = tmp_path / "state"
    db_path = data_dir / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPSHUB_ACTOR", raising=False)
    monkeypatch.delenv("OPSHUB_WORK_SESSION_ID", raising=False)

    return {
        "db_path": db_path,
        "state_home": state_home,
        "state_file": state_home / "opshub" / "current-session",
    }


def _run_init(runner: CliRunner) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout


# ---- session start --------------------------------------------------------


def test_session_start_prints_ulid_and_writes_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["session", "start", "--scope", "phase-2 step 6"])
    assert result.exit_code == 0, result.stdout

    session_id = result.stdout.strip()
    assert len(session_id) == 26
    parse_ulid_timestamp_ms(session_id)

    # State file now points at the new session.
    state_file = paths["state_file"]
    assert state_file.is_file()
    assert state_file.read_text(encoding="utf-8") == session_id

    # Projection has one active row carrying the scope label.
    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(work_sessions_table)).mappings().all()
    finally:
        engine.dispose()
    assert len(rows) == 1
    assert rows[0]["id"] == session_id
    assert rows[0]["state"] == "active"
    assert rows[0]["scope"] == "phase-2 step 6"


def test_session_start_without_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["session", "start"])
    assert result.exit_code == 0, result.stdout

    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            row = conn.execute(select(work_sessions_table)).mappings().one()
    finally:
        engine.dispose()
    assert row["scope"] is None


# ---- session end ----------------------------------------------------------


def test_session_end_with_explicit_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    start = runner.invoke(app, ["session", "start"])
    assert start.exit_code == 0
    session_id = start.stdout.strip()

    end = runner.invoke(app, ["session", "end", session_id, "--summary", "done"])
    assert end.exit_code == 0, end.stdout
    assert session_id in end.stdout

    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            row = conn.execute(select(work_sessions_table)).mappings().one()
    finally:
        engine.dispose()
    assert row["state"] == "ended"
    assert row["summary"] == "done"

    # The state file was cleared because the ended session matched the
    # one tracked there.
    assert not paths["state_file"].exists()


def test_session_end_uses_state_file_when_id_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    start = runner.invoke(app, ["session", "start"])
    assert start.exit_code == 0
    session_id = start.stdout.strip()

    end = runner.invoke(app, ["session", "end"])
    assert end.exit_code == 0, end.stdout
    assert session_id in end.stdout

    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            row = conn.execute(select(work_sessions_table)).mappings().one()
    finally:
        engine.dispose()
    assert row["state"] == "ended"
    assert not paths["state_file"].exists()


def test_session_end_without_state_file_or_id_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["session", "end"])
    assert result.exit_code != 0
    assert result.exception is not None


def test_session_end_explicit_id_preserves_other_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ending a non-current session must not clear the active state file."""
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    first = runner.invoke(app, ["session", "start"])
    assert first.exit_code == 0
    first_id = first.stdout.strip()

    second = runner.invoke(app, ["session", "start"])
    assert second.exit_code == 0
    second_id = second.stdout.strip()
    # The state file now points at the second session.
    assert paths["state_file"].read_text(encoding="utf-8") == second_id

    end_first = runner.invoke(app, ["session", "end", first_id])
    assert end_first.exit_code == 0, end_first.stdout
    # Second session is still active and the state file still tracks it.
    assert paths["state_file"].read_text(encoding="utf-8") == second_id


# ---- session list ---------------------------------------------------------


def test_session_list_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    runner.invoke(app, ["session", "start", "--scope", "alpha"])
    runner.invoke(app, ["session", "start", "--scope", "beta"])

    result = runner.invoke(app, ["session", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload: list[dict[str, Any]] = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 2
    scopes = {row["scope"] for row in payload}
    assert scopes == {"alpha", "beta"}


def test_session_list_md_renders_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    runner.invoke(app, ["session", "start", "--scope", "md scope"])

    result = runner.invoke(app, ["session", "list", "--format", "md"])
    assert result.exit_code == 0, result.stdout
    assert "| ID |" in result.stdout
    assert "| --- |" in result.stdout
    assert "md scope" in result.stdout


def test_session_list_table_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    runner.invoke(app, ["session", "start", "--scope", "table scope"])

    result = runner.invoke(app, ["session", "list"])
    assert result.exit_code == 0, result.stdout
    assert "ID" in result.stdout
    assert "SCOPE" in result.stdout
    assert "table scope" in result.stdout


def test_session_list_invalid_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["session", "list", "--format", "invalid"])
    assert result.exit_code != 0
    assert result.exception is not None


def test_session_list_excludes_ended_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    keep = runner.invoke(app, ["session", "start", "--scope", "keep"])
    assert keep.exit_code == 0
    drop = runner.invoke(app, ["session", "start", "--scope", "drop"])
    assert drop.exit_code == 0
    drop_id = drop.stdout.strip()
    runner.invoke(app, ["session", "end", drop_id])

    result = runner.invoke(app, ["session", "list", "--format", "json"])
    assert result.exit_code == 0
    payload: list[dict[str, Any]] = json.loads(result.stdout)
    scopes = {row["scope"] for row in payload}
    assert scopes == {"keep"}
