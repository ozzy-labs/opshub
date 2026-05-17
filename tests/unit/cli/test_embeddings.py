"""Tests for ``opshub embeddings rebuild`` / ``opshub embeddings status``.

Phase 4 step B3 replaces the Phase 1 placeholder body with two real
commands:

* ``embeddings rebuild`` calls :class:`EmbeddingService.embed_pending`.
* ``embeddings status`` reports the configured backend / model and a
  per-entity-type breakdown of total / embedded / pending counts.

The tests below cover both shapes end-to-end through
:class:`typer.testing.CliRunner`. The CLI wiring is intentionally not
mocked — we go through the full ``build_embedding_service`` /
``build_engine`` / factory path so the integration between the Phase 1
placeholder rewrite, the Phase 4 service (PR #69), and the factory
(PR #68) stays pinned.

Each test isolates paths to ``tmp_path`` via env vars so the user's real
XDG dirs are never touched. The ``OPSHUB_EMBEDDING__BACKEND`` env var
selects the backend; for tests that need a working embedder we
monkeypatch :func:`opshub.vectors.factory.build_embedder` to return a
deterministic stub (the local backend would try to download a 500MB
sentence-transformers model on first call).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import insert, text
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.ids import new_ulid
from opshub.core.time import now_utc
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.decisions import decisions_table
from opshub.projections.sources import sources_table
from opshub.projections.tasks import tasks_table
from opshub.vectors.embedder import EmbeddingResult

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


# ---- fixtures + stubs -----------------------------------------------------


class _StubEmbedder:
    """Tiny embedder stub used by `embeddings rebuild` integration tests.

    The vector is derived from ``len(text)`` so each input maps to a
    distinct (but stable) value — enough for the service to record an
    embedding without touching a real model. Mirrors the embedder stub
    in :mod:`tests.unit.services.test_embedding_service` so the unit
    and CLI suites stay in lock-step on the embedder shape.
    """

    def __init__(
        self,
        *,
        model_id: str = "cli-stub-embedder",
        model_version: str = "v1",
        dim: int = 1024,
    ) -> None:
        self._model_id = model_id
        self._model_version = model_version
        self._dim = dim

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> EmbeddingResult:
        base = float(len(text) % self._dim) / max(self._dim, 1)
        return EmbeddingResult(
            vector=tuple(base + i * 0.1 for i in range(self._dim)),
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


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


def _install_stub_embedder(
    monkeypatch: pytest.MonkeyPatch,
    embedder: _StubEmbedder,
) -> None:
    """Monkeypatch the factory so both wiring and status see the same stub.

    The CLI's ``embeddings status`` resolves the embedder by calling
    :func:`opshub.vectors.factory.build_embedder` directly; the
    ``embeddings rebuild`` wiring helper also goes through the same
    factory. Patching the factory keeps both call sites consistent.
    """
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub_build_embedder(settings: OpsHubSettings) -> Embedder:
        del settings  # unused; the stub does not consult config.
        return embedder

    monkeypatch.setattr(factory_module, "build_embedder", _stub_build_embedder)


def _open_db(db_path: Path) -> Engine:
    """Open a SQLAlchemy engine against the test SQLite database."""
    return create_engine_for_sqlite(db_path)


def _seed_task(engine: Engine, *, title: str) -> str:
    """Insert one row into ``tasks_table`` in the ``draft`` state."""
    task_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(tasks_table).values(
                id=task_id,
                title=title,
                body=None,
                state="draft",
                result_note=None,
                created_at=now,
                updated_at=now,
            )
        )
    return task_id


def _seed_decision(engine: Engine, *, text_value: str) -> str:
    """Insert one row into ``decisions_table``."""
    decision_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(decisions_table).values(
                id=decision_id,
                text=text_value,
                context=None,
                actor="cli:test",
                recorded_at=now,
            )
        )
    return decision_id


def _seed_source(engine: Engine, *, summary: str, external_id: str) -> str:
    """Insert one row into ``sources_table``."""
    source_id = new_ulid()
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            insert(sources_table).values(
                id=source_id,
                connector_name="github",
                external_id=external_id,
                source_type="issue",
                title="placeholder title",
                url=None,
                summary=summary,
                observed_at=now,
                updated_at=now,
            )
        )
    return source_id


def _embeddings_row_count(engine: Engine) -> int:
    """Return total row count of the ``embeddings`` metadata table."""
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM embeddings")).scalar_one())


# ---- rebuild --------------------------------------------------------------


def test_embeddings_rebuild_with_no_data_returns_zero_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty DB → ``embedded 0, skipped 0, failed 0`` and exit 0.

    The bracketing :class:`EmbeddingRebuildRequested` still appends so
    ``rebuild_run_id`` is a real ULID even with no entities to embed.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, _StubEmbedder())
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    result = runner.invoke(app, ["embeddings", "rebuild"])
    assert result.exit_code == 0, result.stdout
    assert "embedded 0" in result.stdout
    assert "skipped 0" in result.stdout
    assert "failed 0" in result.stdout
    assert "rebuild_run_id=" in result.stdout


def test_embeddings_rebuild_embeds_pending_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Seed 1 task, rebuild → embedded 1 in stdout and a row in ``embeddings``."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, _StubEmbedder())
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    engine = _open_db(db_path)
    try:
        _seed_task(engine, title="hello world")
    finally:
        engine.dispose()

    result = runner.invoke(app, ["embeddings", "rebuild"])
    assert result.exit_code == 0, result.stdout
    assert "embedded 1" in result.stdout
    assert "failed 0" in result.stdout

    engine = _open_db(db_path)
    try:
        assert _embeddings_row_count(engine) == 1
    finally:
        engine.dispose()


def test_embeddings_rebuild_with_entity_type_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--entity-type task`` embeds the task, leaves the source pending."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, _StubEmbedder())
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    engine = _open_db(db_path)
    try:
        _seed_task(engine, title="task to embed")
        _seed_source(engine, summary="source stays pending", external_id="ext-1")
    finally:
        engine.dispose()

    result = runner.invoke(app, ["embeddings", "rebuild", "--entity-type", "task"])
    assert result.exit_code == 0, result.stdout
    assert "embedded 1" in result.stdout

    engine = _open_db(db_path)
    try:
        # Only the task row landed; the source is still pending.
        assert _embeddings_row_count(engine) == 1
        with engine.connect() as conn:
            row = conn.execute(text("SELECT entity_type FROM embeddings")).scalar_one()
        assert row == "task"
    finally:
        engine.dispose()


def test_embeddings_rebuild_with_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--limit 2`` caps the number of rows embedded across entity types."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, _StubEmbedder())
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    engine = _open_db(db_path)
    try:
        for i in range(5):
            _seed_task(engine, title=f"task {i}")
    finally:
        engine.dispose()

    result = runner.invoke(app, ["embeddings", "rebuild", "--limit", "2"])
    assert result.exit_code == 0, result.stdout
    assert "embedded 2" in result.stdout

    engine = _open_db(db_path)
    try:
        assert _embeddings_row_count(engine) == 2
    finally:
        engine.dispose()


# ---- status ---------------------------------------------------------------


def test_embeddings_status_disabled_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``backend=disabled`` short-circuits before opening the DB.

    The Phase 4 status path explicitly skips the engine open on
    ``disabled`` so an operator can call ``embeddings status`` straight
    after install (no ``opshub init`` yet) without hitting a
    ConfigError. We exercise that path by NOT running ``opshub init``.
    """
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    # NOTE: no `opshub init` here — the disabled-backend path must not
    # touch the engine, so an uninitialised DB is the strongest test.
    result = runner.invoke(app, ["embeddings", "status"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.startswith("backend=disabled")
    assert "embeddings rebuild" in result.stdout  # hint mentions next step


def test_embeddings_status_active_backend_shows_per_entity_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Active backend: status reports total / embedded / pending per type."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, _StubEmbedder())
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    engine = _open_db(db_path)
    try:
        _seed_task(engine, title="task 1")
        _seed_task(engine, title="task 2")
        _seed_decision(engine, text_value="d1")
    finally:
        engine.dispose()

    # Rebuild first so the embedded counts are non-zero.
    rebuild = runner.invoke(app, ["embeddings", "rebuild"])
    assert rebuild.exit_code == 0, rebuild.stdout

    status = runner.invoke(app, ["embeddings", "status"])
    assert status.exit_code == 0, status.stdout
    assert "backend=local" in status.stdout
    assert "model_id=cli-stub-embedder" in status.stdout
    # All seeded rows should now be embedded (pending=0 for both
    # task / decision; inbox_item / source remain at 0/0).
    # Render is a plain-text table — assert on stable substrings.
    assert "task" in status.stdout
    assert "decision" in status.stdout


def test_embeddings_status_json_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--format json`` emits parseable JSON with the per-entity rows."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, _StubEmbedder())
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    engine = _open_db(db_path)
    try:
        _seed_task(engine, title="t1")
    finally:
        engine.dispose()

    status = runner.invoke(app, ["embeddings", "status", "--format", "json"])
    assert status.exit_code == 0, status.stdout
    # Strip the two-line header (backend / model_id) before parsing.
    lines = status.stdout.strip().splitlines()
    # Last line is the JSON array.
    from typing import cast

    payload = cast(list[dict[str, object]], json.loads(lines[-1]))
    assert isinstance(payload, list)
    by_type: dict[str, dict[str, object]] = {str(row["entity_type"]): row for row in payload}
    assert by_type["task"]["total"] == 1
    assert by_type["task"]["pending"] == 1  # not yet rebuilt
    assert {"task", "decision", "inbox_item", "source"} <= set(by_type)


def test_embeddings_status_md_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--format md`` emits a GitHub Markdown table."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch, _StubEmbedder())
    runner = CliRunner()

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.stdout

    status = runner.invoke(app, ["embeddings", "status", "--format", "md"])
    assert status.exit_code == 0, status.stdout
    assert "| entity_type | total | embedded | pending |" in status.stdout
