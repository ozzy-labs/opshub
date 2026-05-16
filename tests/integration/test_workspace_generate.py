"""End-to-end ``opshub workspace generate`` integration tests.

The full Phase 1 + Phase 2 stack is exercised:

* Alembic-managed schema (via ``opshub init``).
* Real :class:`SqlAlchemyEventStore` + inline projector materialising every
  Phase 2 read model through the service layer.
* :func:`generate_workspace` driving the four renderers
  (tasks / inbox / decisions / handoffs) onto the configured workspace
  tree, one subdirectory per renderer.

The contracts pinned here mirror ADR-0003 ("disposable workspace") and
plan §2.3 step 8 (Markdown coordination rendering + atomic writes):

1. Every row in each projection produces a per-row ``.md`` plus a
   per-renderer ``index.md``.
2. A second call on unchanged state writes zero files (idempotency).
3. Deleting a projection row removes the corresponding ``.md`` (orphan
   cleanup) — scoped per renderer.
4. Writes are atomic: a crash mid-write must not leave a half-written
   final file (only a ``*.tmp`` sibling, if anything).
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
from opshub.projections import (
    DecisionsProjection,
    HandoffsProjection,
    InboxProjection,
    Projection,
    TasksProjection,
    tasks_table,
)
from opshub.services.decision_service import DecisionService
from opshub.services.handoff_service import HandoffService
from opshub.services.inbox_service import InboxService
from opshub.services.task_service import TaskService


class _InlineProjector:
    """Projector that writes through every Phase 2 read model immediately.

    Used by the integration tests so service commands update each
    projection in-line, without going through the rebuild driver. The
    production wiring (Phase 2 step 14's ``cli/_wiring.py``) follows the
    same shape: a fan-out projector that forwards every event to every
    reducer.

    The ``connection`` keyword matches the
    :class:`opshub.services.projector.Projector` Protocol; when the
    service threads in a transaction connection we honour it, otherwise
    we open a short-lived one against the engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._projections: list[Projection] = [
            TasksProjection(),
            InboxProjection(),
            DecisionsProjection(),
            HandoffsProjection(),
        ]

    def apply(self, event: object, connection: object | None = None) -> None:
        from opshub.domain.events.base import DomainEvent  # local import

        assert isinstance(event, DomainEvent)
        if connection is not None:
            for projection in self._projections:
                projection.apply(connection, event)  # type: ignore[arg-type]
            return
        with self._engine.begin() as conn:
            for projection in self._projections:
                projection.apply(conn, event)


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


def _seed_coordination(engine: Engine) -> dict[str, list[str]]:
    """Seed one inbox item, one decision, and one (open) handoff.

    Returns a dict of ``{aggregate: [ids]}`` so each test can pick the
    relevant ids without re-deriving them from the projection tables.
    """
    store = SqlAlchemyEventStore(engine)
    projector = _InlineProjector(engine)

    inbox = InboxService(store=store, projector=projector)
    decisions = DecisionService(store=store, projector=projector)
    handoffs = HandoffService(store=store, projector=projector)

    enq = inbox.enqueue(summary="triage this", source_ref="slack:#ops")
    dec = decisions.record_decision(text="adopt ADR-0003", context="phase 1 outcome")
    ho = handoffs.open(from_actor="alice", to_actor="bob", topic="rotate creds")

    return {
        "inbox": [enq.aggregate_id],
        "decisions": [dec.aggregate_id],
        "handoffs": [ho.aggregate_id],
    }


def _tasks_dir(workspace_root: Path) -> Path:
    return workspace_root / "generated" / "tasks"


def _inbox_dir(workspace_root: Path) -> Path:
    return workspace_root / "generated" / "inbox"


def _decisions_dir(workspace_root: Path) -> Path:
    return workspace_root / "generated" / "decisions"


def _handoffs_dir(workspace_root: Path) -> Path:
    return workspace_root / "generated" / "handoffs"


def test_workspace_generate_creates_index_and_per_task_files(
    initialised_paths: dict[str, Path],
) -> None:
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        ids = _seed_three_tasks(engine)
        written = generate_workspace(engine, initialised_paths["workspace_root"])
    finally:
        engine.dispose()

    out = _tasks_dir(initialised_paths["workspace_root"])
    assert (out / INDEX_FILENAME).is_file()
    for task_id in ids:
        assert (out / f"{task_id}.md").is_file()

    # Each empty renderer still writes its (empty) ``index.md``, so the
    # total write count includes the tasks output plus three empty
    # indexes for inbox / decisions / handoffs.
    assert written == 1 + len(ids) + 3

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
        out = _tasks_dir(initialised_paths["workspace_root"])
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

        out = _tasks_dir(initialised_paths["workspace_root"])
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


def test_generate_workspace_includes_inbox_decisions_handoffs(
    initialised_paths: dict[str, Path],
) -> None:
    """Each new renderer writes its index + per-row .md files."""
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        seeded = _seed_coordination(engine)
        generate_workspace(engine, initialised_paths["workspace_root"])
    finally:
        engine.dispose()

    root = initialised_paths["workspace_root"]

    inbox_dir = _inbox_dir(root)
    assert (inbox_dir / "index.md").is_file()
    for item_id in seeded["inbox"]:
        assert (inbox_dir / f"{item_id}.md").is_file()
    # The inbox index groups by state — every canonical state header
    # must appear, even when its section is empty.
    inbox_index = (inbox_dir / "index.md").read_text(encoding="utf-8")
    for state in ("pending", "triaged_to_task", "triaged_to_decision", "discarded"):
        assert f"## {state}" in inbox_index

    decisions_dir = _decisions_dir(root)
    assert (decisions_dir / "index.md").is_file()
    for dec_id in seeded["decisions"]:
        assert (decisions_dir / f"{dec_id}.md").is_file()
    decisions_index = (decisions_dir / "index.md").read_text(encoding="utf-8")
    for dec_id in seeded["decisions"]:
        assert f"./{dec_id}.md" in decisions_index

    handoffs_dir = _handoffs_dir(root)
    assert (handoffs_dir / "index.md").is_file()
    for h_id in seeded["handoffs"]:
        assert (handoffs_dir / f"{h_id}.md").is_file()
    handoffs_index = (handoffs_dir / "index.md").read_text(encoding="utf-8")
    # Both sections always exist; the seeded handoff is open so it
    # appears under "## Open".
    assert "## Open" in handoffs_index
    assert "## Closed" in handoffs_index
    for h_id in seeded["handoffs"]:
        assert f"./{h_id}.md" in handoffs_index


def test_generate_workspace_deletes_orphans_per_renderer(
    initialised_paths: dict[str, Path],
) -> None:
    """Each renderer cleans up its own subdir; no cross-renderer effects."""
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        _seed_three_tasks(engine)
        _seed_coordination(engine)
        # First generate so each subdir exists.
        generate_workspace(engine, initialised_paths["workspace_root"])

        root = initialised_paths["workspace_root"]
        stale_files = [
            _tasks_dir(root) / "stale-task.md",
            _inbox_dir(root) / "stale-inbox.md",
            _decisions_dir(root) / "stale-decision.md",
            _handoffs_dir(root) / "stale-handoff.md",
        ]
        for path in stale_files:
            path.write_text("manually injected orphan\n", encoding="utf-8")
            assert path.is_file()

        # Second generate must remove every orphan.
        generate_workspace(engine, initialised_paths["workspace_root"])
    finally:
        engine.dispose()

    for path in stale_files:
        assert not path.exists(), f"orphan {path} survived regeneration"


def test_generate_workspace_atomic_write_no_partial_file(
    initialised_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure mid-write must not leave a half-written final file.

    We monkey-patch ``pathlib.Path.write_bytes`` so the first temp-file
    write raises after a partial payload lands. ``_sync_files`` writes
    to ``*.tmp`` first and only ``os.replace``-s into place on success,
    so the canonical ``.md`` must remain whatever it was before the
    crash (here: absent).
    """
    engine = create_engine_for_sqlite(initialised_paths["db_path"])
    try:
        _seed_three_tasks(engine)

        from pathlib import Path as _Path

        real_write_bytes = _Path.write_bytes

        def exploding_write_bytes(self: _Path, data: bytes) -> int:
            # Partially write, then raise — simulates an interrupted
            # write (disk full, SIGKILL between syscall fragments…).
            if self.suffix == ".tmp":
                self.write_text(data.decode("utf-8")[: len(data) // 2], encoding="utf-8")
                raise OSError("simulated interrupted write")
            return real_write_bytes(self, data)

        monkeypatch.setattr(_Path, "write_bytes", exploding_write_bytes)

        with pytest.raises(OSError, match="simulated interrupted write"):
            generate_workspace(engine, initialised_paths["workspace_root"])
    finally:
        engine.dispose()

    out = _tasks_dir(initialised_paths["workspace_root"])
    # Whatever the renderer attempted to write, no canonical .md file
    # exists. ``_sync_files`` removes the partial temp file on failure
    # so the workspace stays clean.
    if out.exists():
        for entry in out.iterdir():
            assert entry.suffix != ".md", f"partial .md leaked: {entry}"
            assert entry.suffix != ".tmp", f"orphan .tmp leaked: {entry}"
