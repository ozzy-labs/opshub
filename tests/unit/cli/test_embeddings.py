"""Tests for ``opshub embeddings status``.

All invocations isolate paths to ``tmp_path`` via env vars so the user's
real XDG dirs are never touched. Each test runs ``opshub init`` (when
applicable) through :class:`typer.testing.CliRunner` so the schema —
including the empty ``embeddings`` table — is in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.errors import ConfigError


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


def test_status_reports_disabled_backend_and_zero_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default backend is ``disabled`` and the embeddings table starts empty."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    status_result = runner.invoke(app, ["embeddings", "status"])
    assert status_result.exit_code == 0, status_result.stdout
    assert "backend=disabled" in status_result.stdout
    assert "embeddings: 0 rows" in status_result.stdout


def test_status_without_init_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running ``embeddings status`` against an uninitialised DB surfaces ConfigError."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["embeddings", "status"])
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)


def test_status_reflects_configured_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``OPSHUB_EMBEDDING__BACKEND=local`` is reflected in the status output."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    status_result = runner.invoke(app, ["embeddings", "status"])
    assert status_result.exit_code == 0, status_result.stdout
    assert "backend=local" in status_result.stdout
    assert "embeddings: 0 rows" in status_result.stdout
