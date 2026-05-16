"""End-to-end ``opshub workspace generate`` integration tests.

The full Phase 1 stack is exercised:

* Alembic-managed schema (via ``opshub init``).
* Real :class:`SqlAlchemyEventStore` + inline projector materialising the
  ``tasks`` read model through :class:`TaskService` calls.
* :func:`generate_workspace` driving the markdown render onto the
  configured workspace tree.

The contracts pinned here mirror ADR-0003 ("disposable workspace"):

1. Every task in the projection produces a per-task ``.md`` plus an
   ``index.md`` listing them.
2. A second call on unchanged state writes zero files (idempotency).
3. Deleting a projection row removes the corresponding ``.md`` (orphan
   cleanup).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.event_store import SqlAlchemyEventStore
from opshub.markdown.tasks import INDEX_FILENAME
from opshub.markdown.workspace import generate_workspace
from opshub.projections import TasksProjection, tasks_table
from opshub.services.task_service import TaskService


class _InlineProjector:
    """Projector that writes through to the ``tasks`` read model immediately.

    Used by the integration tests so that ``TaskService`` commands update
    the projection without going through the rebuild driver. The
    production wiring (which lives in step 14's ``cli/_wiring.py`` task
    service constructor) follows the same shape.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._projection = TasksProjection()

    def apply(self, event: object) -> None:
        # The ``DomainEvent`` shape matches what ``TasksProjection.apply``
        # expects; the cast is intentional so this helper stays free of
        # domain imports.
        from opshub.domain.events.base import DomainEvent  # local import

        assert isinstance(event, DomainEvent)
        with self._engine.begin() as conn:
            self._projection.apply(conn, event)


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point every OpsHub path env var inside ``tmp_path``."""
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


@pytest.fixture
def initialised_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Path]]:
    """Run ``opshub init`` against an isolated tmp tree and yield the paths."""
    paths = _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout
    yield paths


def _seed_three_tasks(engine: Engine) -> list[str]:
    """Create three tasks and advance two of them, returning their ids in order."""
    store = SqlAlchemyEventStore(engine)
    projector = _InlineProjector(engine)
    service = TaskService(store=store, projector=projector)

    a = service.create_task(title="first", body="alpha body")
    b = service.create_task(title="second")
    c = service.create_task(title="third", body="gamma body")

    service.activate_task(a.aggregate_id)
    service.complete_task(b.aggregate_id, "done already")

    return [a.aggregate_id, b.aggregate_id, c.aggregate_id]


def _generated_dir(workspace_root: Path) -> Path:
    return workspace_root / "generated" / "tasks"


def test_workspace_generate_creates_index_and_per_task_files(
    initialised_paths: dict[str, Path],
) -> None:
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        ids = _seed_three_tasks(engine)
        written = generate_workspace(engine, initialised_paths["workspace_root"])
    finally:
        engine.dispose()

    out = _generated_dir(initialised_paths["workspace_root"])
    assert (out / INDEX_FILENAME).is_file()
    for task_id in ids:
        assert (out / f"{task_id}.md").is_file()

    # One write per output file (index + 3 tasks).
    assert written == 1 + len(ids)

    # Index references every task id.
    index_text = (out / INDEX_FILENAME).read_text(encoding="utf-8")
    for task_id in ids:
        assert f"./{task_id}.md" in index_text


def test_workspace_generate_is_idempotent_on_unchanged_state(
    initialised_paths: dict[str, Path],
) -> None:
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        _seed_three_tasks(engine)

        first_written = generate_workspace(engine, initialised_paths["workspace_root"])
        out = _generated_dir(initialised_paths["workspace_root"])
        first_index = (out / INDEX_FILENAME).read_bytes()
        first_mtime = (out / INDEX_FILENAME).stat().st_mtime_ns

        second_written = generate_workspace(engine, initialised_paths["workspace_root"])
        second_index = (out / INDEX_FILENAME).read_bytes()
        second_mtime = (out / INDEX_FILENAME).stat().st_mtime_ns
    finally:
        engine.dispose()

    assert first_written > 0
    # The contract: second run on unchanged state writes nothing.
    assert second_written == 0
    # Byte equality across runs.
    assert first_index == second_index
    # And we did not touch mtime (no spurious filesystem churn).
    assert first_mtime == second_mtime


def test_workspace_generate_removes_orphan_files_when_task_deleted(
    initialised_paths: dict[str, Path],
) -> None:
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        ids = _seed_three_tasks(engine)
        generate_workspace(engine, initialised_paths["workspace_root"])

        out = _generated_dir(initialised_paths["workspace_root"])
        doomed = ids[0]
        assert (out / f"{doomed}.md").is_file()

        # Surgically remove one row from the projection (simulating the
        # canonical scenario: the task vanishes, the workspace must
        # follow).
        with engine.begin() as conn:
            conn.execute(delete(tasks_table).where(tasks_table.c.id == doomed))

        generate_workspace(engine, initialised_paths["workspace_root"])
    finally:
        engine.dispose()

    # The deleted task's file is gone.
    assert not (out / f"{doomed}.md").exists()
    # The other tasks are still present.
    for survivor in ids[1:]:
        assert (out / f"{survivor}.md").is_file()
    # And the index no longer references the deleted id.
    index_text = (out / INDEX_FILENAME).read_text(encoding="utf-8")
    assert doomed not in index_text


def test_cli_workspace_generate_smoke(initialised_paths: dict[str, Path]) -> None:
    """``opshub workspace generate`` exits 0 and reports the file count."""
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        _seed_three_tasks(engine)
    finally:
        engine.dispose()

    runner = CliRunner()
    result = runner.invoke(app, ["workspace", "generate"])
    assert result.exit_code == 0, result.stdout
    assert "wrote" in result.stdout
    # Second invocation: should print "wrote 0 file(s)".
    result2 = runner.invoke(app, ["workspace", "generate"])
    assert result2.exit_code == 0, result2.stdout
    assert "wrote 0 file(s)" in result2.stdout
