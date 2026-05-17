"""Phase 5 end-to-end lifecycle tests.

Drives the Phase 5 briefing flow through the shipped CLI surface with a
mocked :class:`~opshub.llm.client.LLMClient` and a mocked
:class:`~opshub.vectors.embedder.Embedder`. Pattern mirrors
:mod:`tests.integration.test_phase4_lifecycle` (Phase 4 closeout): one
test function per Phase 5 closeout slice, each under ~80 LOC.

``isolated_env`` fixture (``tests/integration/conftest.py``) provisions
``OPSHUB_*`` env, runs ``init``, and yields a paths dict. The whole
module is skipped when ``sqlite_vec`` is not importable
(non-``[vector]`` environments) so contributors who run ``uv sync
--extra dev`` without the vector extras do not trip migration 0013 with
``no such module: vec0``. This mirrors
:mod:`tests.integration.test_phase4_lifecycle`.

What this pins
--------------

The integration test pins the **shipped CLI contract** end-to-end —
not implementation details:

- ``opshub embeddings rebuild`` followed by ``opshub brief "<topic>"``
  succeeds with the mocked LLM, emits ``BriefingRequested`` +
  ``BriefingGenerated`` events, and materialises one ``briefings``
  projection row (Phase 5 plan §3 Sub-issue D bullet #1).
- ``opshub brief "<topic>" --save`` writes the markdown file under
  ``<workspace.root>/briefings/<slug>-<briefing-id>.md`` (Phase 5
  plan §3 Sub-issue B bullet #5, also covered by D1).
- Two sequential briefings on the same topic produce 2 x
  ``BriefingRequested`` + 2 x ``BriefingGenerated`` events with
  distinct ``aggregate_id`` (no overwrite, event-sourced trace
  maintained — Phase 5 plan §1 確定済み事項 #8).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip when sqlite-vec is not installed (matches
# ``test_phase4_lifecycle``): migration 0013 emits
# ``CREATE VIRTUAL TABLE ... USING vec0`` which the ``opshub init``
# step inside ``isolated_env`` runs.
pytest.importorskip("sqlite_vec")

from pydantic import BaseModel
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import events_table
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.projections.briefings import briefings_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs (mocked LLMClient + mocked Embedder; no real network calls)
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub copied from Phase 4 closeout.

    Hashes the text into a unit-L2-normalised vector so identical text
    maps to identical vectors. Mirrors
    :class:`tests.integration.test_phase4_lifecycle._StubEmbedder` —
    we copy the implementation rather than import it to keep the two
    Phase test modules independent (Phase 4 test could be refactored
    without breaking Phase 5).
    """

    def __init__(
        self,
        *,
        model_id: str = "phase5-stub-embedder",
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
        slots = [0.0] * self._dim
        for i, ch in enumerate(text):
            slots[i % self._dim] += (ord(ch) % 31 + 1) / 31.0
        norm = max(sum(x * x for x in slots) ** 0.5, 1e-9)
        vector = tuple(x / norm for x in slots)
        return EmbeddingResult(
            vector=vector,
            model_id=self._model_id,
            model_version=self._model_version,
            dim=self._dim,
        )


class _StubLLMClient:
    """LLMClient stub used end-to-end through the factory monkeypatch.

    Returns a fixed markdown body so the assertions below can pin the
    exact stdout content. ``model_id`` / ``model_version`` are
    surfaced on the persisted ``briefings`` projection row so we can
    also assert the cost-trace columns landed correctly.
    """

    def __init__(
        self,
        *,
        text: str = "# Phase 5 Briefing\n\n- alpha task\n- beta decision",
        model_id: str = "stub-llm-haiku",
        model_version: str = "phase5-test",
        tokens_in: int = 123,
        tokens_out: int = 45,
    ) -> None:
        self._text = text
        self._model_id = model_id
        self._model_version = model_version
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.complete_calls: list[tuple[list[LLMMessage], int]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int,
        temperature: float = 0.2,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        del temperature, stop
        self.complete_calls.append((list(messages), max_tokens))
        return LLMResponse(
            text=self._text,
            model_id=self._model_id,
            model_version=self._model_version,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        # Phase 5 lifecycle tests never call structured output; the stub
        # still has to satisfy the Phase 6 Protocol extension.
        del messages, schema, max_tokens, temperature
        raise NotImplementedError("_StubLLMClient.complete_structured is not used in Phase 5 tests")


def _install_stub_embedder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_id: str = "phase5-stub-embedder",
    dim: int = 1024,
) -> None:
    """Patch :func:`opshub.vectors.factory.build_embedder` to return the stub."""
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub_build_embedder(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder(model_id=model_id, dim=dim)

    monkeypatch.setattr(factory_module, "build_embedder", _stub_build_embedder)


def _install_stub_llm(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubLLMClient,
) -> None:
    """Patch :func:`opshub.llm.factory.build_llm_client` to return the stub.

    The CLI wiring (``build_briefing_service`` in
    :mod:`opshub.cli._wiring`) reaches the LLM client through the
    factory. Patching the factory rather than the wiring helper keeps
    the test agnostic to the wiring helper's internals (and matches
    the Phase 4 ``build_embedder`` patch style).
    """
    from opshub.core.config import OpsHubSettings
    from opshub.llm import factory as factory_module
    from opshub.llm.client import LLMClient

    def _stub_build_llm_client(settings: OpsHubSettings) -> LLMClient:
        del settings
        return stub

    monkeypatch.setattr(factory_module, "build_llm_client", _stub_build_llm_client)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    """Run ``opshub`` via CliRunner and return ``(exit_code, stdout, stderr)``."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Sub-issue D: brief e2e + save markdown
# ---------------------------------------------------------------------------


def test_brief_e2e_happy_path(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``task create`` → ``embeddings rebuild`` → ``brief`` → projection row.

    Drives the full Phase 5 happy path through the shipped CLI:

    1. Seed two tasks and one decision via the CLI.
    2. ``opshub embeddings rebuild`` populates the vector store.
    3. ``opshub brief "<topic>"`` returns markdown on stdout.
    4. ``briefings`` projection has exactly one row keyed by the
       briefing ULID, with the mocked LLM's model_id / tokens.
    5. The event log contains one ``BriefingRequested`` + one
       ``BriefingGenerated`` event sharing the same ``aggregate_id``.
    """
    # Configure the CLI: local embedding backend (stubbed) + anthropic
    # LLM backend (stubbed). The factory patches below intercept both
    # before any real model / SDK is loaded.
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    # ---- 1. seed two tasks + one decision via the CLI --------------------
    code, _, _ = _invoke(["task", "create", "phase 5 alpha task"])
    assert code == 0
    code, _, _ = _invoke(["task", "create", "phase 5 beta task"])
    assert code == 0
    code, _, _ = _invoke(["decision", "record", "phase 5 closeout decision"])
    assert code == 0

    # ---- 2. rebuild embeddings (mocked embedder) -------------------------
    code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out
    assert "embedded 3" in rebuild_out, rebuild_out

    # ---- 3. opshub brief "<topic>" ---------------------------------------
    code, brief_out, stderr = _invoke(["brief", "phase 5 progress"])
    assert code == 0, stderr or brief_out
    # The mocked LLM returns the fixed markdown — it must reach stdout.
    assert "# Phase 5 Briefing" in brief_out, brief_out
    assert len(stub_llm.complete_calls) == 1

    # ---- 4. briefings projection row -------------------------------------
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(briefings_table)).all()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row.topic == "phase 5 progress"
        assert row.model_id == "stub-llm-haiku"
        assert row.tokens_in == 123
        assert row.tokens_out == 45
        assert "# Phase 5 Briefing" in row.markdown
        briefing_id = row.id

        # ---- 5. events log: Requested + Generated, same aggregate_id ----
        with engine.connect() as conn:
            requested_rows = conn.execute(
                select(events_table).where(events_table.c.event_type == "briefing.requested")
            ).all()
            generated_rows = conn.execute(
                select(events_table).where(events_table.c.event_type == "briefing.generated")
            ).all()
        assert len(requested_rows) == 1
        assert len(generated_rows) == 1
        assert requested_rows[0].aggregate_id == briefing_id
        assert generated_rows[0].aggregate_id == briefing_id
    finally:
        engine.dispose()


def test_brief_save_writes_markdown_under_workspace(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``opshub brief --save`` writes ``<workspace>/briefings/<slug>-<id>.md``.

    Re-runs the brief flow with ``--save`` and verifies the markdown
    file lands under the tmp-redirected workspace root with the
    documented filename convention (slug-id.md). The slug is
    deterministic from the topic so we assert its prefix; the ULID
    suffix is pinned via the ``briefings`` projection row.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient(text="# Saved\n\nbody")
    _install_stub_llm(monkeypatch, stub_llm)

    # Seed one task so the recall hit list is non-empty (the briefing
    # would still work with zero sources, but a non-empty hit list
    # exercises the source-text load path inside BriefingService).
    code, _, _ = _invoke(["task", "create", "save-target task"])
    assert code == 0
    code, _, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0

    code, stdout, stderr = _invoke(["brief", "save target topic", "--save"])
    assert code == 0, stderr or stdout
    # ``--save`` echoes the saved path on stderr so a piped stdout
    # stays clean markdown.
    assert "saved briefing to" in stderr, stderr

    # Look up the briefing_id from the projection so we can resolve
    # the exact filename. ``slugify`` lower-cases + collapses spaces
    # into hyphens, so "save target topic" → "save-target-topic".
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(briefings_table)).all()
        assert len(rows) == 1
        briefing_id = rows[0].id
    finally:
        engine.dispose()

    target = isolated_env["workspace_root"] / "briefings" / f"save-target-topic-{briefing_id}.md"
    assert target.exists(), f"expected briefing file at {target}"
    assert target.read_text(encoding="utf-8") == "# Saved\n\nbody"


def test_brief_repeated_runs_emit_distinct_briefings(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ``brief`` invocations → 2 Requested + 2 Generated, distinct ids.

    Phase 5 plan §1 確定済み事項 #8: regenerate is a *new* briefing
    record, not an overwrite. The event log must retain both
    attempts and the projection must hold both rows so the audit
    trail is preserved.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "openai")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient(model_id="stub-llm-gpt4o-mini")
    _install_stub_llm(monkeypatch, stub_llm)

    code, _, _ = _invoke(["task", "create", "repeat-topic seed task"])
    assert code == 0
    code, _, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0

    code1, _, _ = _invoke(["brief", "same topic"])
    assert code1 == 0
    code2, _, _ = _invoke(["brief", "same topic"])
    assert code2 == 0
    # Two LLM calls, no cache.
    assert len(stub_llm.complete_calls) == 2

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table.c.aggregate_id).where(
                    events_table.c.event_type == "briefing.requested"
                )
            ).all()
            generated = conn.execute(
                select(events_table.c.aggregate_id).where(
                    events_table.c.event_type == "briefing.generated"
                )
            ).all()
            briefing_ids = conn.execute(select(briefings_table.c.id)).all()
        assert len(requested) == 2
        assert len(generated) == 2
        # Distinct ids — no overwrite.
        assert len({row.aggregate_id for row in requested}) == 2
        assert len({row.aggregate_id for row in generated}) == 2
        assert len({row.id for row in briefing_ids}) == 2
    finally:
        engine.dispose()


def test_brief_json_format_emits_full_record(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``opshub brief --format json`` emits a JSON record with the documented keys.

    Pins the JSON shape exported by
    :func:`opshub.cli._render.render_briefing_json` — Phase 5.x
    ``opshub brief history --format json`` will piggyback on the
    same schema, so this test is the canonical lock.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch, _StubLLMClient(text="# JSON body\n"))

    code, _, _ = _invoke(["task", "create", "json fixture task"])
    assert code == 0
    code, _, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0

    code, stdout, _ = _invoke(["brief", "json topic", "--format", "json"])
    assert code == 0, stdout
    payload = json.loads(stdout)
    expected_keys = {
        "briefing_id",
        "topic",
        "scope",
        "model_id",
        "model_version",
        "tokens_in",
        "tokens_out",
        "source_refs",
        "markdown",
        "generated_at",
    }
    assert set(payload.keys()) == expected_keys, set(payload.keys())
    assert payload["topic"] == "json topic"
    assert payload["model_id"] == "stub-llm-haiku"
    assert payload["markdown"] == "# JSON body\n"


# Re-export ``pytest`` so static analysers see this module is a pytest test
# (the import would otherwise read as unused).
_ = pytest
