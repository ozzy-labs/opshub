"""Tests for ``opshub agent run begin`` / ``opshub agent run end``.

Every test isolates the CLI invocation via ``monkeypatch.setenv`` so the
user's real ``~/.config/opshub`` / ``~/.local/share/opshub`` /
``~/.local/state/opshub`` directories are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.ids import parse_ulid_timestamp_ms
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.agent_runs import agent_runs_table


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point every OpsHub path env var inside ``tmp_path``."""
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

    return {"db_path": db_path, "state_home": state_home}


def _run_init(runner: CliRunner) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout


# ---- agent run begin ------------------------------------------------------


def test_agent_run_begin_prints_ulid_and_persists_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["agent", "run", "begin", "claude"])
    assert result.exit_code == 0, result.stdout

    run_id = result.stdout.strip()
    assert len(run_id) == 26
    parse_ulid_timestamp_ms(run_id)

    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(agent_runs_table)).mappings().all()
    finally:
        engine.dispose()
    assert len(rows) == 1
    assert rows[0]["id"] == run_id
    assert rows[0]["state"] == "active"
    assert rows[0]["agent_name"] == "claude"
    assert rows[0]["work_session_id"] is None


def test_agent_run_begin_inherits_session_from_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--session`` the state-file ``current-session`` is used."""
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    start = runner.invoke(app, ["session", "start"])
    assert start.exit_code == 0
    session_id = start.stdout.strip()

    begin = runner.invoke(app, ["agent", "run", "begin", "claude"])
    assert begin.exit_code == 0, begin.stdout
    run_id = begin.stdout.strip()

    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(select(agent_runs_table).where(agent_runs_table.c.id == run_id))
                .mappings()
                .one()
            )
    finally:
        engine.dispose()
    assert row["work_session_id"] == session_id


def test_agent_run_begin_session_flag_overrides_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    start = runner.invoke(app, ["session", "start"])
    assert start.exit_code == 0
    # state-file session is whatever ``session start`` wrote, but
    # ``--session`` must win.

    override_id = "01HZZZZZZZZZZZZZZZZZZZZZ99"
    begin = runner.invoke(
        app,
        ["agent", "run", "begin", "claude", "--session", override_id],
    )
    assert begin.exit_code == 0, begin.stdout
    run_id = begin.stdout.strip()

    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(select(agent_runs_table).where(agent_runs_table.c.id == run_id))
                .mappings()
                .one()
            )
    finally:
        engine.dispose()
    assert row["work_session_id"] == override_id


def test_agent_run_begin_rejects_empty_agent_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["agent", "run", "begin", ""])
    assert result.exit_code != 0


# ---- agent run end --------------------------------------------------------


def test_agent_run_end_transitions_row_to_ended(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    begin = runner.invoke(app, ["agent", "run", "begin", "claude"])
    assert begin.exit_code == 0
    run_id = begin.stdout.strip()

    end = runner.invoke(app, ["agent", "run", "end", run_id, "--summary", "done"])
    assert end.exit_code == 0, end.stdout
    assert run_id in end.stdout

    engine = create_engine_for_sqlite(paths["db_path"])
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(select(agent_runs_table).where(agent_runs_table.c.id == run_id))
                .mappings()
                .one()
            )
    finally:
        engine.dispose()
    assert row["state"] == "ended"
    assert row["summary"] == "done"


def test_agent_run_end_rejects_non_ulid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    _run_init(runner)

    result = runner.invoke(app, ["agent", "run", "end", "not-a-ulid"])
    assert result.exit_code != 0
