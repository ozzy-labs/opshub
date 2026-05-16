"""Tests for ``opshub projections rebuild``.

Every test isolates the CLI invocation via ``monkeypatch.setenv`` so the
user's real XDG dirs are never touched. The DB is initialised by
running ``opshub init`` via :class:`typer.testing.CliRunner` so the test
exercises the same wiring path the user sees.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.errors import ConfigError
from opshub.db import SqlAlchemyEventStore, create_engine_for_sqlite
from opshub.projections import tasks_table
from opshub.services.projector import NoOpProjector
from opshub.services.task_service import TaskService


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


def _seed_two_tasks(db_path: Path) -> None:
    """Append two ``TaskCreated`` events through the real service stack.

    The rebuild command derives the read-model from the event log directly,
    so a :class:`NoOpProjector` is enough while seeding — we let the
    subsequent ``projections rebuild`` invocation materialise the ``tasks``
    table from scratch.
    """
    engine = create_engine_for_sqlite(db_path)
    try:
        store = SqlAlchemyEventStore(engine)
        service = TaskService(store, NoOpProjector())
        service.create_task("first task")
        service.create_task("second task", body="with a body")
    finally:
        engine.dispose()


def _count_task_rows(db_path: Path) -> int:
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return len(list(conn.execute(select(tasks_table.c.id))))
    finally:
        engine.dispose()


def test_rebuild_reports_event_and_projection_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After seeding two tasks, rebuild reports ``2 event(s)`` and the active projections count."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_two_tasks(db_path)

    rebuild_result = runner.invoke(app, ["projections", "rebuild"])
    assert rebuild_result.exit_code == 0, rebuild_result.stdout
    # Stdout must surface both the event count and the projection count.
    # Number of projections grows as Phase 2 adds inbox/decisions/etc.,
    # so just verify the format strings are present.
    assert "2 event(s)" in rebuild_result.stdout
    assert "projection(s)" in rebuild_result.stdout

    # The projection table now mirrors the event log.
    assert _count_task_rows(db_path) == 2


def test_rebuild_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two consecutive rebuilds produce identical projection row counts."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    _seed_two_tasks(db_path)

    first = runner.invoke(app, ["projections", "rebuild"])
    assert first.exit_code == 0, first.stdout
    rows_after_first = _count_task_rows(db_path)

    second = runner.invoke(app, ["projections", "rebuild"])
    assert second.exit_code == 0, second.stdout
    rows_after_second = _count_task_rows(db_path)

    assert rows_after_first == rows_after_second == 2


def test_rebuild_without_init_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running ``projections rebuild`` against an uninitialised DB surfaces ConfigError."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["projections", "rebuild"])
    assert result.exit_code != 0
    # CliRunner records the raised exception on ``result.exception``.
    assert isinstance(result.exception, ConfigError)
