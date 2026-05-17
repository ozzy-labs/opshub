"""Phase 6 end-to-end lifecycle tests (ADR-0016).

Drives the Phase 6 Action loop flow through the shipped CLI surface
with a mocked :class:`~opshub.llm.client.LLMClient` (structured output)
and a mocked :class:`~opshub.vectors.embedder.Embedder`. Pattern
mirrors :mod:`tests.integration.test_phase5_lifecycle` (Phase 5
closeout); we extend the Phase 5 stubs rather than re-implement them.

What this pins
--------------

- ``opshub task create`` / ``opshub decision record`` → ``opshub
  embeddings rebuild`` → ``opshub brief "<topic>"`` → ``opshub propose
  generate "<topic>" --from-briefing <briefing_id>`` succeeds with a
  mocked LLM and emits ``ProposalRequested`` + ``ProposalGenerated``
  events sharing the same ``aggregate_id`` (Phase 6 plan §3 Sub-issue
  C bullet #1).
- The proposals projection materialises one row with
  ``candidate_states == ["pending", "pending"]``.
- ``opshub propose apply <id> 0`` creates a real task via the existing
  :class:`TaskService` (ADR-0016 §決定 (g)), emits ``TaskCreated``
  alongside ``ProposalApplied``, and flips
  ``candidate_states[0]`` to ``"applied"``.
- ``opshub propose reject <id> 1 --reason ...`` emits a
  ``ProposalRejected`` event with the reason and flips
  ``candidate_states[1]`` to ``"rejected"``.
- ``opshub propose list`` filters on candidate state membership so a
  proposal whose remaining candidates are all applied / rejected
  disappears from ``--state pending`` but still surfaces from the
  unfiltered listing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip when sqlite-vec is not installed (matches
# :mod:`tests.integration.test_phase5_lifecycle`).
pytest.importorskip("sqlite_vec")

from pydantic import BaseModel
from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import events_table
from opshub.domain.events.proposal import (
    DecisionCandidatePayload,
    TaskCandidatePayload,
)
from opshub.llm.client import LLMMessage, LLMResponse, StructuredResponse
from opshub.projections.proposals import proposals_table
from opshub.projections.tasks import tasks_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs (mocked LLMClient + mocked Embedder; no real network calls).
# Kept independent from the Phase 5 stubs so refactoring one phase's
# test fixtures cannot break another.
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub — same shape as Phase 5 lifecycle."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase6-stub-embedder"

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
    """LLMClient stub that drives BOTH the brief and propose paths.

    ``complete`` returns a fixed markdown briefing body.
    ``complete_structured`` returns a fixed two-candidate proposal
    (1 task + 1 decision) so the apply / reject lifecycle has both
    candidate kinds covered.
    """

    def __init__(
        self,
        *,
        brief_text: str = "# Phase 6 Briefing\n\n- alpha task\n- beta decision",
        task_title: str = "tighten phase 6 docs",
        task_body: str | None = "follow-up from briefing",
        decision_text: str = "adopt ollama as the local LLM default",
        decision_context: str | None = "phase 6 closeout",
        model_id: str = "stub-llm-haiku",
        model_version: str = "phase6-test",
        tokens_in: int = 120,
        tokens_out: int = 80,
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
        # ``schema`` is :class:`ProposalCandidatesSchema` per the
        # service contract; construct it from the canned candidates so
        # the caller's Pydantic validation path matches the production
        # one (Anthropic / OpenAI clients do the same parse + validate
        # dance).
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
        # The stub is structurally a :class:`LLMClient` (Protocol is
        # ``@runtime_checkable``); mypy needs a hint because the stub
        # class itself does not inherit from the Protocol.
        return stub  # type: ignore[return-value,unused-ignore]

    monkeypatch.setattr(factory_module, "build_llm_client", _builder)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


def test_propose_lifecycle_generate_apply_reject_through_cli(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the full Phase 6 Action loop through ``opshub propose``.

    Sequence (mirrors Phase 6 plan §2.4 C1):

    1. Seed a couple of tasks and one decision via the CLI.
    2. ``opshub embeddings rebuild`` (mocked embedder).
    3. ``opshub brief "<topic>"`` to mint a briefing the propose
       command can consume via ``--from-briefing``.
    4. ``opshub propose generate --from-briefing <id>`` → 2 candidates
       (1 task + 1 decision) rendered to stdout; events log holds
       ``ProposalRequested`` + ``ProposalGenerated`` with shared
       ``aggregate_id``; ``proposals`` projection row has
       ``candidate_states == ["pending", "pending"]``.
    5. ``opshub propose apply <proposal_id> 0`` creates a real task
       through :class:`TaskService` (verify ``TaskCreated`` +
       ``ProposalApplied`` events, ``tasks`` projection row, and the
       updated ``candidate_states``).
    6. ``opshub propose reject <proposal_id> 1 --reason ...`` records
       ``ProposalRejected`` and flips the second candidate to
       ``"rejected"``.
    7. ``opshub propose list --state pending`` is empty because the
       only proposal has no pending candidates left, while ``opshub
       propose list`` still surfaces the row.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    stub_llm = _StubLLMClient()
    _install_stub_llm(monkeypatch, stub_llm)

    # ---- 1. seed two tasks + one decision via the CLI --------------------
    code, _, _ = _invoke(["task", "create", "phase 6 alpha task"])
    assert code == 0
    code, _, _ = _invoke(["task", "create", "phase 6 beta task"])
    assert code == 0
    code, _, _ = _invoke(["decision", "record", "phase 6 closeout decision"])
    assert code == 0

    # ---- 2. rebuild embeddings ------------------------------------------
    code, rebuild_out, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0, rebuild_out

    # ---- 3. brief: gives us a briefing id to seed propose ---------------
    code, brief_out, _ = _invoke(["brief", "phase 6 action loop", "--format", "json"])
    assert code == 0, brief_out
    brief_payload = json.loads(brief_out)
    briefing_id = brief_payload["briefing_id"]
    assert len(stub_llm.complete_calls) == 1

    # ---- 4. propose generate --------------------------------------------
    code, generate_out, _ = _invoke(
        [
            "propose",
            "generate",
            "phase 6 next steps",
            "--from-briefing",
            briefing_id,
        ]
    )
    assert code == 0, generate_out
    assert "[0] task:" in generate_out, generate_out
    assert "[1] decision:" in generate_out, generate_out
    assert "tighten phase 6 docs" in generate_out, generate_out
    assert len(stub_llm.structured_calls) == 1
    # Capture the proposal id from the rendered output. The CLI prints
    # ``Proposal: <ulid>`` on its own line; we extract that ULID to
    # drive the apply / reject calls below.
    proposal_id: str | None = None
    for line in generate_out.splitlines():
        if line.startswith("Proposal:"):
            proposal_id = line.split(":", 1)[1].strip()
            break
    assert proposal_id is not None, generate_out
    assert len(proposal_id) == 26

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # 4a. projection row + candidate_states.
        with engine.connect() as conn:
            rows = conn.execute(select(proposals_table)).all()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row.id == proposal_id
        assert row.topic == "phase 6 next steps"
        assert list(row.candidate_states) == ["pending", "pending"]
        assert row.model_id == "stub-llm-haiku"

        # 4b. events: Requested + Generated share aggregate_id ----------
        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.requested")
            ).all()
            generated = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.generated")
            ).all()
        assert len(requested) == 1
        assert len(generated) == 1
        assert requested[0].aggregate_id == proposal_id
        assert generated[0].aggregate_id == proposal_id

        # ---- 5. apply candidate 0 (task) -------------------------------
        before_tasks = _count_tasks(engine)
        code, apply_out, _ = _invoke(["propose", "apply", proposal_id, "0"])
        assert code == 0, apply_out
        assert "created task:" in apply_out, apply_out
        after_tasks = _count_tasks(engine)
        assert after_tasks == before_tasks + 1

        # 5a. candidate_states[0] flipped to "applied".
        with engine.connect() as conn:
            updated = conn.execute(
                select(proposals_table.c.candidate_states).where(
                    proposals_table.c.id == proposal_id
                )
            ).scalar_one()
        assert list(updated) == ["applied", "pending"]

        # 5b. ProposalApplied event has the new task ULID.
        with engine.connect() as conn:
            applied_events = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.applied")
            ).all()
        assert len(applied_events) == 1
        applied_payload = json.loads(applied_events[0].payload)
        assert applied_payload["candidate_index"] == 0
        assert applied_payload["applied_entity_type"] == "task"
        new_task_id = applied_payload["applied_entity_id"]
        assert len(new_task_id) == 26

        # 5c. TaskCreated event for the same task id is also durable
        # (apply path routed through TaskService — ADR-0016 §決定 (g)).
        with engine.connect() as conn:
            task_created = conn.execute(
                select(events_table).where(
                    (events_table.c.event_type == "task.created")
                    & (events_table.c.aggregate_id == new_task_id)
                )
            ).all()
        assert len(task_created) == 1

        # 5d. tasks projection row exists with the LLM-supplied title.
        with engine.connect() as conn:
            task_row = conn.execute(
                select(tasks_table.c.title, tasks_table.c.body).where(
                    tasks_table.c.id == new_task_id
                )
            ).first()
        assert task_row is not None
        assert task_row.title == "tighten phase 6 docs"
        assert task_row.body == "follow-up from briefing"

        # ---- 6. reject candidate 1 (decision) --------------------------
        code, reject_out, _ = _invoke(
            ["propose", "reject", proposal_id, "1", "--reason", "out of scope"]
        )
        assert code == 0, reject_out
        assert "Rejected candidate" in reject_out, reject_out

        with engine.connect() as conn:
            rejected_events = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.rejected")
            ).all()
        assert len(rejected_events) == 1
        rejected_payload = json.loads(rejected_events[0].payload)
        assert rejected_payload["candidate_index"] == 1
        assert rejected_payload["reason"] == "out of scope"

        with engine.connect() as conn:
            final_states = conn.execute(
                select(proposals_table.c.candidate_states).where(
                    proposals_table.c.id == proposal_id
                )
            ).scalar_one()
        assert list(final_states) == ["applied", "rejected"]

        # ---- 7. list filters ------------------------------------------
        code, pending_out, _ = _invoke(["propose", "list", "--state", "pending"])
        assert code == 0, pending_out
        # The proposal has no pending candidate left so the pending
        # bucket should not surface it. The ``propose list`` markdown
        # view shortens the ULID to its 6-char ``git log --oneline``
        # style prefix.
        short_id = proposal_id[:6]
        assert short_id not in pending_out

        code, all_out, _ = _invoke(["propose", "list"])
        assert code == 0, all_out
        # Default listing still shows it (6-char id prefix).
        assert short_id in all_out
    finally:
        engine.dispose()


def _count_tasks(engine: object) -> int:
    """Return the current number of rows in the ``tasks`` projection."""
    from sqlalchemy.engine import Engine

    typed = engine if isinstance(engine, Engine) else None
    assert typed is not None, "engine must be a SQLAlchemy Engine"
    with typed.connect() as conn:
        rows = conn.execute(select(tasks_table.c.id)).all()
    return len(rows)


# Re-export ``pytest`` so static analysers see this module is a pytest test.
_ = pytest
