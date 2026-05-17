"""End-to-end ``opshub recall`` integration test (Phase 4 step C2).

Drives the freshly-shipped CLI surface through the same
``isolated_env`` fixture used by the rest of the integration suite:

1. ``opshub init`` provisions the schema (via the fixture).
2. A task row is seeded directly into the projection table.
3. ``opshub embeddings rebuild`` embeds the task via a stub
   :class:`~opshub.vectors.embedder.Embedder` (a real local backend
   would download a 500MB sentence-transformers model on first call).
4. ``opshub recall "<query>"`` is invoked through the same stub
   embedder; the test asserts the seeded task appears in stdout.

The whole module is skipped when ``sqlite_vec`` is not importable
(non-``[vector]`` environments) so contributors who run ``uv sync
--extra dev`` without the vector extras don't trip the migration with
``no such module: vec0``. This mirrors :mod:`test_phase4_migrations`.

What this pins
--------------

The integration test pins the **shipped CLI contract** end-to-end —
not implementation details. Specifically it asserts:

* ``backend=disabled`` short-circuits exits 2 with the setup hint
  (smoke-tests the fast path before opening the engine).
* With an active backend + a rebuilt embedding, ``recall`` exits 0 and
  prints the seeded task's title.
* ``--format json`` round-trips through :mod:`opshub.cli._render` and
  emits a parseable JSON list with the documented keys.

The unit suite (:mod:`tests.unit.cli.test_recall`) covers the CLI
shape in isolation via stubs for both embedder *and* recall service;
this module exercises the full wiring graph (factory → embedder →
sqlite-vec → projections → renderer) once so an end-to-end regression
is caught by a single failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip when sqlite-vec is not installed (matches ``test_phase4_migrations``).
# Migration 0013 emits ``CREATE VIRTUAL TABLE ... USING vec0`` which the
# ``opshub init`` step inside ``isolated_env`` runs; without the
# extension that migration fails before any test body executes.
pytest.importorskip("sqlite_vec")

import json
from typing import cast

from sqlalchemy import insert
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.ids import new_ulid
from opshub.core.time import now_utc
from opshub.db.engine import create_engine_for_sqlite
from opshub.projections.tasks import tasks_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---- stubs ----------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub used end-to-end.

    Mirrors :class:`tests.unit.cli.test_embeddings._StubEmbedder` so
    the integration test inherits the same model identity (``model_id`` /
    ``model_version``) the unit suite uses — keeping the recall and
    rebuild paths in lock-step on backend metadata even when the real
    local backend is not available.

    The vector is derived from ``hash(text) % dim`` so two calls with
    the same input produce the same vector; that is the contract the
    sqlite-vec store needs for a rebuild → recall round trip.
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
        # A simple deterministic vector: distance-from-base scales with
        # text length. The exact values are irrelevant — the recall
        # smoke-test only needs the same vector for the same input on
        # both the rebuild and the recall paths.
        base = float(len(text) % self._dim) / max(self._dim, 1)
        return EmbeddingResult(
            vector=tuple(base + i * 0.1 for i in range(self._dim)),
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :func:`opshub.vectors.factory.build_embedder` to return the stub.

    Both the recall wiring (:func:`build_recall_service`) and the
    rebuild wiring (:func:`build_embedding_service`) reach the embedder
    through the same factory; patching it once keeps the two call sites
    aligned on the same ``(model_id, model_version)``.
    """
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub_build_embedder(settings: OpsHubSettings) -> Embedder:
        del settings  # unused — the stub does not consult config.
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub_build_embedder)


# ---- helpers --------------------------------------------------------------


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Run ``opshub`` via CliRunner and return ``(exit_code, stdout, stderr)``."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


def _seed_task(db_path: Path, *, title: str) -> str:
    """Insert one ``draft`` task row directly into the projection table."""
    engine = create_engine_for_sqlite(db_path)
    try:
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
    finally:
        engine.dispose()


# ---- tests ----------------------------------------------------------------


def test_recall_disabled_backend_short_circuits(isolated_env: _PathsDict) -> None:
    """``backend=disabled`` exits 2 with the setup hint before opening DB.

    ``isolated_env`` runs ``opshub init`` (so the engine *would* open),
    but :class:`OpsHubSettings` keeps ``backend=disabled`` by default —
    the recall command must short-circuit before any DB / embedder
    work.
    """
    # Sanity-check the fixture actually provisioned the env we expect:
    # the integration suite shares the same fixture, so a regression
    # would also break the workspace + coordination lifecycle tests.
    assert isolated_env["db_path"].exists()

    exit_code, stdout, stderr = _invoke(["recall", "anything"])
    assert exit_code == 2, stdout
    assert "Embedding backend is disabled" in stderr
    assert "opshub embeddings rebuild" in stderr


def test_recall_end_to_end_finds_seeded_task(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: _PathsDict,
) -> None:
    """Seed a task, rebuild embeddings, then recall the task by free-form query.

    Pins the full Phase 4 MVP pipeline: factory → embedder →
    sqlite-vec round trip → projection JOIN → :mod:`opshub.cli._render`
    table.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    seeded_title = "hello opshub world"
    _seed_task(isolated_env["db_path"], title=seeded_title)

    rebuild_exit, rebuild_stdout, _ = _invoke(["embeddings", "rebuild"])
    assert rebuild_exit == 0, rebuild_stdout
    assert "embedded 1" in rebuild_stdout

    recall_exit, recall_stdout, recall_stderr = _invoke(["recall", "hello world", "--type", "task"])
    assert recall_exit == 0, recall_stderr or recall_stdout
    # The seeded task's title must appear in the rendered table.
    assert seeded_title in recall_stdout


def test_recall_json_format_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: _PathsDict,
) -> None:
    """``--format json`` emits a parseable JSON list with documented keys."""
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    seeded_title = "another seeded title"
    _seed_task(isolated_env["db_path"], title=seeded_title)

    rebuild_exit, _, _ = _invoke(["embeddings", "rebuild"])
    assert rebuild_exit == 0

    recall_exit, recall_stdout, _ = _invoke(["recall", "anything", "--format", "json"])
    assert recall_exit == 0, recall_stdout

    payload = cast(
        list[dict[str, object]],
        json.loads(recall_stdout.strip().splitlines()[-1]),
    )
    assert isinstance(payload, list)
    assert len(payload) >= 1
    assert set(payload[0].keys()) == {"score", "entity_type", "entity_id", "title"}
    assert payload[0]["title"] == seeded_title
    assert payload[0]["entity_type"] == "task"
