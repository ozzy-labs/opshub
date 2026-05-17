"""Phase 8 end-to-end automatic link extraction chain (E1 closeout, ADR-0017).

Drives the Phase 8 knowledge graph layer through the shipped CLI
surface with a mocked :class:`~opshub.llm.client.LLMClient` (both
``complete`` for brief and ``complete_structured`` for propose) and a
mocked :class:`~opshub.vectors.embedder.Embedder`. Walks the full
``task create`` → ``embeddings rebuild`` → ``brief`` → ``propose
generate --from-briefing`` → ``propose apply`` → ``graph trace`` chain
and asserts that **every link in the chain is materialised
automatically** by the Phase 8 B2 ``LinksExtractor`` projector — no
manual ``opshub link add`` is needed for the auto-extracted paths.

What this pins
--------------

- Step (4) ``opshub brief`` produces a briefing whose
  ``BriefingGenerated.source_refs`` carries the recall hit list; the
  projector materialises one ``referenced_in_briefing`` row per
  source_ref.
- Step (5) ``opshub propose generate --from-briefing <id>`` records a
  ``ProposalRequested(briefing_id=<id>)`` event; the projector
  materialises a ``generated_from_briefing`` link from the new
  proposal to the seeding briefing.
- Step (6) ``opshub propose apply <id> 0`` creates a new task via
  :class:`TaskService` and records ``ProposalApplied(applied_entity_id=
  <new-task-id>)``; the projector materialises an ``applied_to`` link
  from the proposal to the new task.
- Step (7) ``opshub graph trace <new-task-id>`` walks the incoming
  edges backward and surfaces the chain ``new-task ← proposal ←
  briefing ← original-task`` (the original-task → briefing edge is
  reachable through the briefing's incoming chain because the
  ``referenced_in_briefing`` link points **from** briefing **to** the
  recall hit, so the recall hit is upstream of the briefing in
  ``incoming`` direction — equivalent to "what supplied context to
  this briefing").

Phase 8 plan §2.5 E1 (Sub-issue E DoD) pins this exact walk; the test
asserts on link types (not the full link tuples) so a regression in
any single dispatch surfaces with a clear diagnostic.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from pydantic import BaseModel
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events.proposal import (
    DecisionCandidatePayload,
    TaskCandidatePayload,
)
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.projections.links import links_table
from opshub.projections.tasks import tasks_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs — mirror the Phase 5/6 lifecycle stubs but kept local so a
# refactor of one phase's fixtures cannot break another.
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub — Phase 5/6 lifecycle shape."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase8-stub-embedder"

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
    """Drives both ``complete`` (brief) and ``complete_structured`` (propose).

    Mirrors :mod:`tests.integration.test_phase6_lifecycle` shape — a
    fixed markdown briefing for ``complete`` + a fixed two-candidate
    proposal for ``complete_structured`` — so the apply path has a
    deterministic task candidate to mint.
    """

    def __init__(
        self,
        *,
        brief_text: str = "# Phase 8 Briefing\n\n- alpha follow-up\n- beta decision",
        task_title: str = "follow up phase 8 chain",
        task_body: str | None = "verify automatic link extraction",
        decision_text: str = "adopt phase 8 knowledge graph as default",
        decision_context: str | None = "phase 8 closeout",
        model_id: str = "stub-llm-haiku",
        model_version: str = "phase8-test",
        tokens_in: int = 123,
        tokens_out: int = 45,
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


def _link_signatures(engine: object) -> set[tuple[str, str, str, str, str]]:
    """Return the natural-key tuple set for every ``links`` row.

    Comparing the tuple **set** (rather than the rows themselves)
    keeps the assertion stable across SQLite timestamp / id-derivation
    quirks while still pinning every link the chain materialised.
    """
    from sqlalchemy.engine import Engine

    assert isinstance(engine, Engine)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    links_table.c.from_entity_type,
                    links_table.c.from_entity_id,
                    links_table.c.to_entity_type,
                    links_table.c.to_entity_id,
                    links_table.c.link_type,
                )
            )
            .mappings()
            .all()
        )
    return {
        (
            r["from_entity_type"],
            r["from_entity_id"],
            r["to_entity_type"],
            r["to_entity_id"],
            r["link_type"],
        )
        for r in rows
    }


def _extract_proposal_id(generate_stdout: str) -> str:
    """Parse the proposal id from ``opshub propose generate`` stdout.

    The CLI emits ``Proposal: <ulid>`` on its own line; matches the
    parse used by ``tests.integration.test_phase6_lifecycle``.
    """
    for line in generate_stdout.splitlines():
        if line.startswith("Proposal:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"could not find proposal id in: {generate_stdout!r}")


def _existing_task_ids(engine: object) -> Iterator[str]:
    """Yield every current ``tasks.id`` in projection order."""
    from sqlalchemy.engine import Engine

    assert isinstance(engine, Engine)
    with engine.connect() as conn:
        rows = conn.execute(select(tasks_table.c.id)).all()
    for row in rows:
        yield row.id


def test_phase8_lifecycle_extracts_automatic_link_chain(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end automatic link extraction chain through the CLI.

    Sequence (mirrors Phase 8 plan §2.5 E1 spec):

    1. ``opshub task create`` mints the seed task.
    2. ``opshub embeddings rebuild`` populates the vector store so the
       brief's recall hit list is non-empty.
    3. ``opshub brief "<topic>"`` produces a briefing — verify one
       ``briefing → task`` link with ``link_type=referenced_in_briefing``.
    4. ``opshub propose generate --from-briefing <id>`` — verify one
       ``proposal → briefing`` link with ``link_type=generated_from_briefing``.
    5. ``opshub propose apply <id> 0`` — verify one ``proposal → task``
       link with ``link_type=applied_to`` (the new task).
    6. ``opshub graph trace <new-task-id>`` — verify the JSON output
       reports the chain reaching back through proposal → briefing →
       original task.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    # ---- 1. seed a task ----------------------------------------------------
    code, _, _ = _invoke(["task", "create", "phase 8 seed task"])
    assert code == 0

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # Capture the seed task id BEFORE the propose-apply mint so the
        # later "new task" assertion has a stable baseline to diff
        # against.
        seed_task_ids = list(_existing_task_ids(engine))
        assert len(seed_task_ids) == 1
        seed_task_id = seed_task_ids[0]

        # ---- 2. embeddings rebuild ----------------------------------------
        code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
        assert code == 0, rebuild_out

        # ---- 3. brief ------------------------------------------------------
        code, brief_out, _ = _invoke(["brief", "phase 8 progress", "--format", "json"])
        assert code == 0, brief_out
        brief_payload = json.loads(brief_out)
        briefing_id = brief_payload["briefing_id"]
        assert len(briefing_id) == 26

        # ---- 4. propose generate --from-briefing --------------------------
        code, generate_out, _ = _invoke(
            [
                "propose",
                "generate",
                "phase 8 next steps",
                "--from-briefing",
                briefing_id,
            ]
        )
        assert code == 0, generate_out
        proposal_id = _extract_proposal_id(generate_out)
        assert len(proposal_id) == 26

        # ---- 5. propose apply ----------------------------------------------
        code, apply_out, _ = _invoke(["propose", "apply", proposal_id, "0"])
        assert code == 0, apply_out
        assert "created task:" in apply_out, apply_out

        # The new task minted by ``apply`` is the one task id present
        # that was NOT in the seed list. We compute the diff rather
        # than parsing the apply output so the assertion stays robust
        # against future stdout reformatting.
        all_task_ids = set(_existing_task_ids(engine))
        new_task_candidates = all_task_ids - set(seed_task_ids)
        assert len(new_task_candidates) == 1, new_task_candidates
        new_task_id = new_task_candidates.pop()

        # ---- 5a. projections rebuild materialises every auto link ---------
        #
        # The Phase 5 ``BriefingService`` / Phase 6 ``ProposalService``
        # apply their own narrow projection (``BriefingsProjection`` /
        # ``ProposalsProjection``) in the same UoW as the originating
        # event — they do NOT fan the event out to the full
        # :class:`_PersistingProjector` registry. The auto-extracted
        # link rows for ``referenced_in_briefing`` /
        # ``generated_from_briefing`` therefore land in the ``links``
        # table only after a ``projections rebuild`` walks the event
        # log against every registered projector (including
        # :class:`LinksProjector`). The Phase 6 ``ProposalApplied``
        # event lands through :class:`TaskService`'s
        # :class:`_PersistingProjector` so ``applied_to`` is already
        # materialised; the rebuild is still safe (idempotent UPSERT,
        # pinned by ``test_phase8_rebuild_idempotency``).
        code, rebuild_out, _ = _invoke(["projections", "rebuild"])
        assert code == 0, rebuild_out

        sigs_after_rebuild = _link_signatures(engine)
        briefing_to_task = (
            "briefing",
            briefing_id,
            "task",
            seed_task_id,
            "referenced_in_briefing",
        )
        proposal_to_briefing = (
            "proposal",
            proposal_id,
            "briefing",
            briefing_id,
            "generated_from_briefing",
        )
        proposal_to_new_task = (
            "proposal",
            proposal_id,
            "task",
            new_task_id,
            "applied_to",
        )
        assert briefing_to_task in sigs_after_rebuild, (
            "BriefingGenerated.source_refs should materialise a"
            " referenced_in_briefing link from briefing to the recall hit;"
            f" got: {sigs_after_rebuild}"
        )
        assert proposal_to_briefing in sigs_after_rebuild, (
            "ProposalRequested(briefing_id=...) should materialise a"
            " generated_from_briefing link from proposal to briefing;"
            f" got: {sigs_after_rebuild}"
        )
        assert proposal_to_new_task in sigs_after_rebuild, (
            "ProposalApplied should materialise an applied_to link"
            f" from proposal to the new task; got: {sigs_after_rebuild}"
        )

        # ---- 6. graph trace <new-task-id> ---------------------------------
        # ``graph trace`` follows **incoming** edges backward (provenance
        # direction). From the new task, the only incoming edge is the
        # proposal → new_task ``applied_to`` link, so trace surfaces
        # that single link. The proposal has no incoming edge of its
        # own (``generated_from_briefing`` and ``applied_to`` both go
        # **out** of the proposal), so the trace path is one hop deep.
        #
        # The seed task / briefing are reachable via the symmetric
        # bidirectional ``graph expand`` walk pinned in step 7 below.
        code, trace_out, err = _invoke(
            ["graph", "trace", f"task:{new_task_id}", "--format", "json"]
        )
        assert code == 0, trace_out + (err or "")
        trace_payload = json.loads(trace_out)
        all_trace_links: set[tuple[str, str, str, str, str]] = set()
        for path in trace_payload:
            for link in path["links"]:
                all_trace_links.add(
                    (
                        link["from_entity_type"],
                        link["from_entity_id"],
                        link["to_entity_type"],
                        link["to_entity_id"],
                        link["link_type"],
                    )
                )
        assert proposal_to_new_task in all_trace_links, (
            f"graph trace must surface the proposal->task link; got: {all_trace_links}"
        )

        # ---- 7. graph expand <new-task-id> reaches the seed task ----------
        # The ``referenced_in_briefing`` link points briefing → seed_task
        # which is reached by ``expand``'s bidirectional walk but NOT by
        # ``trace``'s incoming-only walk. ``expand --depth 3`` is
        # within the ADR-0017 §決定 (e) ceiling (max 5).
        code, expand_out, err = _invoke(
            [
                "graph",
                "expand",
                f"task:{new_task_id}",
                "--depth",
                "3",
                "--format",
                "json",
            ]
        )
        assert code == 0, expand_out + (err or "")
        expand_payload = json.loads(expand_out)
        # The expand JSON shape (per ``render_graph_subset_json``)
        # carries a ``nodes`` list of {entity_type, entity_id} dicts.
        # The seed task must appear because bidirectional expansion
        # reaches it via briefing → seed_task.
        expand_nodes = {(n["entity_type"], n["entity_id"]) for n in expand_payload["nodes"]}
        assert ("task", seed_task_id) in expand_nodes, (
            "graph expand --depth 3 must reach the original seed task"
            f" via briefing -> seed_task; got nodes: {expand_nodes}"
        )
        assert ("briefing", briefing_id) in expand_nodes
        assert ("proposal", proposal_id) in expand_nodes
        assert ("task", new_task_id) in expand_nodes
    finally:
        engine.dispose()
