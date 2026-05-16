"""Tests for ``opshub init``.

Every test isolates the CLI invocation via ``monkeypatch.setenv`` so that
the user's real ``~/.config/opshub`` / ``~/.local/share/opshub`` directories
are never touched. All paths point inside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.cli.init import STARTER_CONFIG_TOML
from opshub.db.engine import create_engine_for_sqlite


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point every OpsHub path env var inside ``tmp_path`` and return them."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

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


def _assert_events_table(db_path: Path) -> None:
    """Open the SQLite file and assert the ``events`` table exists."""
    engine = create_engine_for_sqlite(db_path)
    try:
        inspector = inspect(engine)
        assert "events" in inspector.get_table_names()
    finally:
        engine.dispose()


def test_init_creates_dirs_config_and_runs_migrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout

    # Every directory was created.
    assert paths["config_dir"].is_dir()
    assert paths["data_dir"].is_dir()
    assert paths["workspace_root"].is_dir()
    assert paths["db_path"].parent.is_dir()

    # Starter config was written.
    config_file = paths["config_dir"] / "config.toml"
    assert config_file.is_file()
    assert config_file.read_text(encoding="utf-8") == STARTER_CONFIG_TOML

    # Alembic ran to head -> SQLite file exists and has the events table.
    assert paths["db_path"].is_file()
    _assert_events_table(paths["db_path"])


def test_init_is_idempotent_and_preserves_user_edits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    # First run installs the starter config.
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.stdout

    # Simulate user customisation of the TOML file.
    config_file = paths["config_dir"] / "config.toml"
    user_edited = STARTER_CONFIG_TOML + '\n# user-added comment\nfoo = "bar"\n'
    config_file.write_text(user_edited, encoding="utf-8")

    # Second run must not overwrite the user's edits.
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0, second.stdout
    assert config_file.read_text(encoding="utf-8") == user_edited


def test_init_force_overwrites_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.stdout

    config_file = paths["config_dir"] / "config.toml"
    config_file.write_text("# clobbered by user\n", encoding="utf-8")

    forced = runner.invoke(app, ["init", "--force"])
    assert forced.exit_code == 0, forced.stdout
    assert config_file.read_text(encoding="utf-8") == STARTER_CONFIG_TOML
