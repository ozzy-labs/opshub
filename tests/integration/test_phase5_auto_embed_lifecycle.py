"""Phase 5 step C1: event-driven auto-embed end-to-end through the CLI.

Demonstrates that ``[embedding] auto = true`` populates the embeddings
projection without an explicit ``opshub embeddings rebuild``, and that
a subsequent ``opshub brief`` discovers the auto-embedded entity via
the semantic recall path. Mirrors the focused unit tests in
:mod:`tests.unit.services.test_auto_embed_hook` but at the CLI surface
so the wiring composition (``cli/_wiring.py``) is also verified.

What this pins
--------------

- ``[embedding] auto = true`` + a working embedder backend → the
  AutoEmbedHook is wired into ``TaskService`` so a single
  ``opshub task create ...`` call leaves the entity embedded.
- The subsequent ``opshub brief "<topic>"`` (mocked LLM) finds that
  task as a recall hit and persists a briefing row whose
  ``source_refs`` references the auto-embedded task.

Kept intentionally small — the auto-embed hook has dedicated unit
tests in :mod:`tests.unit.services.test_auto_embed_hook`. This module
is the "wires up correctly through the CLI" smoke test only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip when sqlite-vec is not installed.
pytest.importorskip("sqlite_vec")

from pydantic import BaseModel
from sqlalchemy import select, text
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.projections.briefings import briefings_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


class _StubEmbedder:
    """Deterministic embedder stub matching the lifecycle test shape."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase5-autoembed-stub"

    @property
    def model_version(self) -> str:
        return "v1"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> EmbeddingResult:
        slots = [0.0] * self._dim
        for i, ch in enumerate(text):
            slots[i % self._dim] += (ord(ch) % 31 + 1) / 31.0
        norm = max(sum(x * x for x in slots) ** 0.5, 1e-9)
        return EmbeddingResult(
            vector=tuple(x / norm for x in slots),
            model_id=self.model_id,
            model_version=self.model_version,
            dim=self._dim,
        )


class _StubLLMClient:
    """LLMClient stub returning a fixed body."""

    @property
    def model_id(self) -> str:
        return "stub-llm-autoembed"

    @property
    def model_version(self) -> str:
        return "v1"

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        del messages, max_tokens, temperature, stop
        return LLMResponse(
            text="# Auto-embed briefing\n",
            model_id=self.model_id,
            model_version=self.model_version,
            tokens_in=20,
            tokens_out=10,
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        # Phase 5 tests never exercise structured output; the stub still
        # has to satisfy the Phase 6 Protocol extension.
        del messages, schema, max_tokens, temperature
        raise NotImplementedError("_StubLLMClient.complete_structured is not used in Phase 5 tests")


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.llm import factory as factory_module
    from opshub.llm.client import LLMClient

    def _builder(settings: OpsHubSettings) -> LLMClient:
        del settings
        return _StubLLMClient()

    monkeypatch.setattr(factory_module, "build_llm_client", _builder)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Auto-embed + briefing happy path
# ---------------------------------------------------------------------------


def test_auto_embed_populates_embeddings_then_brief_finds_task(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``task create`` with ``[embedding] auto = true`` → embed without rebuild.

    1. Enable auto-embed via env (``OPSHUB_EMBEDDING__AUTO=true``) +
       local backend (stubbed).
    2. ``opshub task create "..."`` once. No explicit
       ``opshub embeddings rebuild``.
    3. Verify the ``embeddings`` projection table has one row for the
       new task — the auto-embed hook fired post-commit.
    4. ``opshub brief "<topic>"`` finds the task via recall and
       persists a briefing row whose source_refs include the task.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_EMBEDDING__AUTO", "true")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch)

    # ---- 1. task create — no manual rebuild --------------------------------
    code, task_id_out, _ = _invoke(["task", "create", "auto-embed phase 5 task"])
    assert code == 0, task_id_out
    task_id = task_id_out.strip()

    # ---- 2. embeddings table populated by the auto-embed hook --------------
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT entity_type, entity_id FROM embeddings WHERE entity_id = :eid"),
                {"eid": task_id},
            ).all()
        assert len(rows) == 1, f"expected 1 auto-embedded row for {task_id}, got {rows}"
        assert rows[0].entity_type == "task"

        # ---- 3. opshub brief finds the auto-embedded task ------------------
        code, brief_out, stderr = _invoke(["brief", "auto-embed phase 5 task"])
        assert code == 0, stderr or brief_out
        assert "# Auto-embed briefing" in brief_out

        # ---- 4. briefing projection row references the task ----------------
        with engine.connect() as conn:
            briefing_rows = conn.execute(select(briefings_table)).all()
        assert len(briefing_rows) == 1
        # ``source_refs`` round-trips through SQLAlchemy's JSON adapter
        # as a list of [entity_type, entity_id] pairs.
        source_refs: list[list[str]] = briefing_rows[0].source_refs
        assert isinstance(source_refs, list)
        matched = any(ref[0] == "task" and ref[1] == task_id for ref in source_refs)
        assert matched, f"expected task {task_id} in source_refs, got {source_refs}"
    finally:
        engine.dispose()


# Re-export ``pytest`` for static analysers.
_ = pytest
