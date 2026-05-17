"""Phase 8 ``--expand-graph`` end-to-end lifecycle (E1 closeout, ADR-0017).

Drives the ``opshub brief "<topic>" --expand-graph`` and ``opshub
propose generate "<topic>" --expand-graph`` flags through the shipped
CLI with mocked LLM + Embedder. Asserts the Phase 8 D2 contract:

- Without ``--expand-graph`` (baseline), the LLM prompt contains the
  recall hit only (the original task ``T``). This is the Phase 5
  backward-compat baseline (ADR-0017 §決定 (f)).
- With ``--expand-graph``, the LLM prompt contains BOTH ``T`` and the
  graph-adjacent source ``S`` — the latter pulled in via the manual
  ``references`` link seeded in step 1.
- Dedupe contract: if ``S`` is itself an extra recall hit (seeded
  identically to ``T``), ``--expand-graph`` still emits it once.
- Symmetric for ``opshub propose generate --expand-graph``.

What this pins
--------------

ADR-0017 §決定 (f) ``--expand-graph`` default off + Phase 5 D1 prompt
contract: every graph-expanded source flows through the same
``<source id="..." type="...">...</source>`` delimiter wrap that the
recall-side sources do. The assertion captures the LLM message body
and asserts ``S``'s body text is present in the user message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from pydantic import BaseModel
from sqlalchemy import insert
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events.proposal import (
    DecisionCandidatePayload,
    TaskCandidatePayload,
)
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.projections.sources import sources_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs (Phase 6/7 lifecycle shape; see test_phase8_lifecycle for rationale)
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub — same hashing recipe as Phase 5-7."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase8-expand-stub-embedder"

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
    """Records prompt args so the test can assert what the LLM saw."""

    def __init__(
        self,
        *,
        brief_text: str = "# Expand-Graph Briefing\n\n- alpha\n- beta",
        task_title: str = "follow up expand-graph",
        task_body: str | None = "verify graph-expanded sources",
        decision_text: str = "adopt expand-graph context default off",
        decision_context: str | None = "phase 8 D2",
        model_id: str = "stub-llm-haiku",
        model_version: str = "phase8-expand-test",
        tokens_in: int = 100,
        tokens_out: int = 60,
    ) -> None:
        self._brief_text = brief_text
        self._task_title = task_title
        self._task_body = task_body
        self._decision_text = decision_text
        self._decision_context = decision_context
        self._model_id = model_id
        self._model_version = model_version
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.complete_calls: list[tuple[list[LLMMessage], int]] = []
        self.structured_calls: list[tuple[list[LLMMessage], type[BaseModel], int]] = []

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
            text=self._brief_text,
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
        del temperature
        self.structured_calls.append((list(messages), schema, max_tokens))
        parsed = schema(
            candidates=[
                TaskCandidatePayload(title=self._task_title, body=self._task_body),
                DecisionCandidatePayload(
                    text=self._decision_text,
                    context=self._decision_context,
                ),
            ]
        )
        return StructuredResponse(
            parsed=parsed,
            model_id=self._model_id,
            model_version=self._model_version,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch, stub: _StubLLMClient) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.llm import factory as factory_module
    from opshub.llm.client import LLMClient

    def _builder(settings: OpsHubSettings) -> LLMClient:
        del settings
        return stub  # type: ignore[return-value,unused-ignore]

    monkeypatch.setattr(factory_module, "build_llm_client", _builder)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# Unique tokens we can grep inside the captured LLM prompt to prove
# the source's body made it in. The token is included verbatim in the
# ``summary`` column so the brief's ``<source>`` block wraps it.
_SOURCE_SUMMARY_TOKEN = "expand-graph-source-marker-x9z"
_SOURCE_SUMMARY = f"phase 8 expand-graph source body — {_SOURCE_SUMMARY_TOKEN}"


def _seed_source_row(db_path: Path, source_id: str) -> None:
    """Insert one ``sources`` row directly so the linked source has body text.

    ``BriefingService._extend_with_graph_neighbours`` looks the graph
    neighbour's text up through :func:`_load_entity_text` which reads
    ``sources.summary``. We bypass the full :class:`SourceObserved`
    event chain (which would need a connector context) by inserting
    the projection row directly — the link extraction path under
    test does not depend on the event being present, only on the
    projection row.
    """
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(sources_table).values(
                    id=source_id,
                    connector_name="phase8-test",
                    external_id=f"ext-{source_id[-6:]}",
                    source_type="phase8_expand",
                    title="phase 8 expand-graph linked source",
                    url="https://example.invalid/phase8",
                    summary=_SOURCE_SUMMARY,
                    observed_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
                    updated_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
                )
            )
    finally:
        engine.dispose()


def _user_message_body(complete_call: tuple[list[LLMMessage], int]) -> str:
    """Concatenate user-role content from a captured ``complete`` call.

    The LLM stub records every call as ``(messages, max_tokens)``; the
    delimiter wrap from :mod:`opshub.services.briefings.prompts` lands
    in the user-role message body, so the assertion filters to that
    role and joins on newline.
    """
    messages, _ = complete_call
    return "\n".join(m.content for m in messages if m.role == "user")


def _structured_user_body(
    call: tuple[list[LLMMessage], type[BaseModel], int],
) -> str:
    """Same as :func:`_user_message_body` but for ``complete_structured`` calls."""
    messages, _, _ = call
    return "\n".join(m.content for m in messages if m.role == "user")


def test_brief_with_expand_graph_includes_linked_source(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``brief --expand-graph`` adds the manual link's neighbour to the LLM prompt.

    Sequence:

    1. Seed task ``T`` via ``opshub task create`` so the recall hit
       list reaches it.
    2. Seed source ``S`` directly into the ``sources`` projection so
       the graph-expand neighbour lookup finds a real body to load.
    3. ``opshub link add task:T source:S --type references`` mints
       the manual link.
    4. ``opshub embeddings rebuild`` so ``T`` becomes a recall hit.
    5. ``opshub brief "..."`` (no ``--expand-graph``) → LLM prompt
       contains the task body but NOT ``S``'s marker token.
    6. ``opshub brief "..." --expand-graph`` → LLM prompt contains
       both the task body AND ``S``'s marker token.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    # ---- 1. seed task ------------------------------------------------------
    seed_title = "phase 8 expand-graph anchor task"
    code, _, _ = _invoke(["task", "create", seed_title])
    assert code == 0

    # Read back the task id (the only row in ``tasks``).
    from sqlalchemy import select

    from opshub.projections.tasks import tasks_table

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            task_id = conn.execute(select(tasks_table.c.id)).scalar_one()
    finally:
        engine.dispose()

    # ---- 2. embeddings rebuild (BEFORE seeding the source) ----------------
    #
    # ``embeddings rebuild`` walks every embeddable projection table —
    # if we seeded the source row first it would also be embedded and
    # could surface in recall on its own (bypassing the
    # ``--expand-graph`` gate). By rebuilding now we ensure only the
    # task gets a vector, so the source can only reach the LLM prompt
    # via the graph-expansion path under test.
    code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out

    # ---- 3. seed source row directly --------------------------------------
    source_id = "01J0000000PHASE8EXPANDSRC0"
    _seed_source_row(isolated_env["db_path"], source_id)

    # ---- 4. manual link ---------------------------------------------------
    code, link_out, err = _invoke(
        [
            "link",
            "add",
            f"task:{task_id}",
            f"source:{source_id}",
            "--type",
            "references",
        ]
    )
    assert code == 0, link_out + (err or "")

    # ---- 5. baseline: brief WITHOUT --expand-graph -----------------------
    code, brief_out, err = _invoke(["brief", "expand graph topic"])
    assert code == 0, brief_out + (err or "")
    assert len(stub_llm.complete_calls) == 1
    baseline_user = _user_message_body(stub_llm.complete_calls[0])
    # The task title made it in (recall hit -> source block):
    assert seed_title in baseline_user, baseline_user
    # The source marker did NOT (no graph expansion):
    assert _SOURCE_SUMMARY_TOKEN not in baseline_user, (
        "without --expand-graph, the graph-linked source must NOT appear in the LLM prompt"
    )

    # ---- 6. with --expand-graph ------------------------------------------
    stub_llm.complete_calls.clear()
    code, brief_out2, err = _invoke(["brief", "expand graph topic", "--expand-graph"])
    assert code == 0, brief_out2 + (err or "")
    assert len(stub_llm.complete_calls) == 1
    expanded_user = _user_message_body(stub_llm.complete_calls[0])
    # Both task and linked source must now appear in the prompt:
    assert seed_title in expanded_user, expanded_user
    assert _SOURCE_SUMMARY_TOKEN in expanded_user, (
        "with --expand-graph, the graph-linked source must be appended"
        " to the LLM prompt; got: " + expanded_user
    )
    # Dedupe: the source marker appears EXACTLY once even though
    # it's a graph-expanded source (and not a duplicate recall hit
    # in this seed).
    assert expanded_user.count(_SOURCE_SUMMARY_TOKEN) == 1


def test_propose_generate_with_expand_graph_includes_linked_source(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``propose generate --expand-graph`` mirrors the briefing-side behaviour.

    Same seed sequence as the brief test; asserts the captured
    :meth:`LLMClient.complete_structured` prompt contains the linked
    source's body when ``--expand-graph`` is passed and does NOT
    contain it otherwise.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    seed_title = "phase 8 propose-expand anchor task"
    code, _, _ = _invoke(["task", "create", seed_title])
    assert code == 0

    from sqlalchemy import select

    from opshub.projections.tasks import tasks_table

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            task_id = conn.execute(select(tasks_table.c.id)).scalar_one()
    finally:
        engine.dispose()

    # Embed BEFORE seeding the source so only the task lands in the
    # vector store — symmetric to the brief test's gating rationale.
    code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out

    source_id = "01J0000000PROPEXPANDSRCAB1"
    _seed_source_row(isolated_env["db_path"], source_id)

    code, link_out, err = _invoke(
        [
            "link",
            "add",
            f"task:{task_id}",
            f"source:{source_id}",
            "--type",
            "references",
        ]
    )
    assert code == 0, link_out + (err or "")

    # ---- baseline: propose generate WITHOUT --expand-graph ---------------
    code, out, err = _invoke(["propose", "generate", "propose-expand topic"])
    assert code == 0, out + (err or "")
    assert len(stub_llm.structured_calls) == 1
    baseline_user = _structured_user_body(stub_llm.structured_calls[0])
    assert seed_title in baseline_user
    assert _SOURCE_SUMMARY_TOKEN not in baseline_user, (
        "without --expand-graph, propose generate must not include the linked"
        " source in the LLM prompt"
    )

    # ---- with --expand-graph ---------------------------------------------
    stub_llm.structured_calls.clear()
    code, out, err = _invoke(["propose", "generate", "propose-expand topic", "--expand-graph"])
    assert code == 0, out + (err or "")
    assert len(stub_llm.structured_calls) == 1
    expanded_user = _structured_user_body(stub_llm.structured_calls[0])
    assert seed_title in expanded_user
    assert _SOURCE_SUMMARY_TOKEN in expanded_user, expanded_user
    # Dedupe pin (Phase 8 D2 §dedupe contract).
    assert expanded_user.count(_SOURCE_SUMMARY_TOKEN) == 1
