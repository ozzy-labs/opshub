"""Tests for ``opshub db migrate``.

All invocations isolate paths to ``tmp_path`` via env vars so the user's
real XDG dirs are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OpsHub env vars at ``tmp_path`` and return the SQLite path."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))

    return db_path


def _assert_events_table(db_path: Path) -> None:
    engine = create_engine_for_sqlite(db_path)
    try:
        inspector = inspect(engine)
        assert "events" in inspector.get_table_names()
    finally:
        engine.dispose()


def test_db_migrate_after_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``db migrate`` is a no-op when ``init`` already brought the schema to head."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    migrate_result = runner.invoke(app, ["db", "migrate"])
    assert migrate_result.exit_code == 0, migrate_result.stdout

    _assert_events_table(db_path)


def test_db_migrate_without_prior_init_creates_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running ``db migrate`` against a fresh tmp dir still creates the schema.

    The engine factory creates the SQLite parent directory on demand, so
    callers who run ``db migrate`` before ``init`` still get a working DB
    (the XDG dirs and config.toml just won't be set up).
    """
    db_path = _isolate_env(monkeypatch, tmp_path)
    assert not db_path.exists()

    runner = CliRunner()
    result = runner.invoke(app, ["db", "migrate"])
    assert result.exit_code == 0, result.stdout

    assert db_path.is_file()
    _assert_events_table(db_path)
