"""Phase 6 propose atomicity + failure-path integration tests.

Mirrors :mod:`tests.integration.test_phase5_briefing_atomicity` but
exercises the proposal-specific contracts pinned by ADR-0016
§決定 (d) (idempotent apply / reject) + the
:class:`ProposalService` UoW shape introduced in Phase 6 step B3.

What this pins
--------------

- **LLM failure during generate** → exit code 1, ``ProposalRequested``
  durable, ``ProposalFailed`` durable with sanitised message,
  ``proposals`` projection has zero rows for that proposal id
  (Phase 6 plan §3 Sub-issue C bullet #2).
- **Projector failure during generate** → the same UoW rolls back so
  neither ``ProposalGenerated`` nor the projection row lands; the
  bracket ``ProposalRequested`` survives because it commits in an
  earlier UoW.
- **NoOpLLMClient (backend = disabled)** → exit code 2 + actionable
  hint on stderr (``[llm] backend is disabled``). The CLI
  short-circuits before any event is appended.
- **Already-applied candidate re-apply** → exit code 1, message
  "candidate 0 already applied", no second ``TaskCreated`` event,
  ``candidate_states[0]`` stays "applied".
- **Already-applied candidate reject** → exit code 1, message
  "candidate 0 already applied", no ``ProposalRejected`` event.
- **Cross-proposal duplicate (current intended behaviour pin)** → two
  proposals generated independently from the same seeded source, each
  applied once, mint two distinct tasks (different ULIDs). There is no
  cross-proposal dedup: ADR-0016 §決定 (d) idempotency is scoped to
  ``(proposal_id, candidate_index)`` and HITL review (ADR-0016 §決定
  (c)) is the only line of defence. See #500 / #501.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip when sqlite-vec is not installed (matches
# :mod:`tests.integration.test_phase5_briefing_atomicity`).
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
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs (copied from the Phase 6 lifecycle test so the two modules stay
# independent — refactoring one MUST NOT break the other).
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase6-atomicity-embedder"

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


class _FailingStructuredLLMClient:
    """LLMClient stub that always raises on ``complete_structured``.

    ``complete`` is implemented because the Phase 5 brief path uses
    it; ``complete_structured`` raises the configured exception so the
    propose path records ``ProposalFailed`` and re-raises.
    """

    def __init__(self, *, message: str = "rate limited") -> None:
        self._message = message

    @property
    def model_id(self) -> str:
        return "stub-llm-failing"

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
            text="# Stub Briefing\n",
            model_id=self.model_id,
            model_version=self.model_version,
            tokens_in=1,
            tokens_out=1,
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        del messages, schema, max_tokens, temperature
        raise RuntimeError(self._message)


class _StubLLMClient:
    """LLMClient stub that returns a fixed candidate list (success path)."""

    def __init__(
        self,
        *,
        candidates: list[TaskCandidatePayload | DecisionCandidatePayload] | None = None,
    ) -> None:
        if candidates is None:
            candidates = [
                TaskCandidatePayload(title="phase 6 follow-up task"),
                DecisionCandidatePayload(text="phase 6 follow-up decision"),
            ]
        self._candidates = candidates

    @property
    def model_id(self) -> str:
        return "stub-llm-ok"

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
            text="# Stub Briefing\n",
            model_id=self.model_id,
            model_version=self.model_version,
            tokens_in=10,
            tokens_out=5,
        )

    def complete_structured(
        self,
        messages: list[LLMMessage],
        *,
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float = 0.2,
    ) -> StructuredResponse[BaseModel]:
        del messages, max_tokens, temperature
        parsed = schema(candidates=list(self._candidates))
        return StructuredResponse(
            parsed=parsed,
            model_id=self.model_id,
            model_version=self.model_version,
            tokens_in=50,
            tokens_out=20,
        )


def _install_stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.vectors import factory as factory_module
    from opshub.vectors.embedder import Embedder

    def _stub(settings: OpsHubSettings) -> Embedder:
        del settings
        return _StubEmbedder()

    monkeypatch.setattr(factory_module, "build_embedder", _stub)


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch, stub: object) -> None:
    from opshub.core.config import OpsHubSettings
    from opshub.llm import factory as factory_module
    from opshub.llm.client import LLMClient

    def _builder(settings: OpsHubSettings) -> LLMClient:
        del settings
        return stub  # type: ignore[return-value]

    monkeypatch.setattr(factory_module, "build_llm_client", _builder)


def _invoke(args: list[str]) -> tuple[int, str, str]:
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


def _generate_proposal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    topic: str = "atomicity topic",
) -> str:
    """Drive ``opshub propose generate`` with the success stub.

    Returns the proposal ULID parsed from the rendered markdown so the
    callers can chain apply / reject calls.
    """
    code, generate_out, stderr = _invoke(["propose", "generate", topic])
    assert code == 0, stderr or generate_out
    proposal_id: str | None = None
    for line in generate_out.splitlines():
        if line.startswith("Proposal:"):
            proposal_id = line.split(":", 1)[1].strip()
            break
    assert proposal_id is not None, generate_out
    return proposal_id


def _seed_for_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a task + rebuild embeddings so the recall path is healthy."""
    code, _, _ = _invoke(["task", "create", "atomicity seed task"])
    assert code == 0
    code, _, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0


# ---------------------------------------------------------------------------
# 1. LLM failure during generate
# ---------------------------------------------------------------------------


def test_generate_llm_failure_records_failed_event_no_projection_row(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM raises → exit 1, Requested + Failed events, zero proposal rows."""
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch, _FailingStructuredLLMClient())

    _seed_for_proposal(monkeypatch)

    code, _, stderr = _invoke(["propose", "generate", "failing topic"])
    # ``ProposalService`` re-raises the LLM RuntimeError after recording
    # ``ProposalFailed``; the CLI maps non-OpsHubError exceptions through
    # Typer's default handler so the exit code is non-zero.
    assert code != 0, stderr

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.requested")
            ).all()
            failed = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.failed")
            ).all()
            generated = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.generated")
            ).all()
            rows = conn.execute(select(proposals_table)).all()
        # Bracket + failure events durable, sharing aggregate_id.
        assert len(requested) == 1, requested
        assert len(failed) == 1, failed
        assert requested[0].aggregate_id == failed[0].aggregate_id
        # No success event, no projection row — atomicity holds.
        assert len(generated) == 0, generated
        assert rows == [], rows
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. Projector failure during ProposalGenerated apply
# ---------------------------------------------------------------------------


def test_generate_projector_failure_rolls_back_generated_event(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projector apply failure rolls back the ProposalGenerated UoW.

    :class:`ProposalService` commits ``store.append(ProposalGenerated)``
    + ``projector.apply(ProposalGenerated)`` inside one UoW (the
    ``engine.begin()`` context manager). When the projector raises, the
    UoW rolls back and neither the event nor the projection row lands.
    The bracketing ``ProposalRequested`` event uses a separate UoW and
    commits normally — mirroring the Phase 5 briefing rollback shape.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch, _StubLLMClient())

    from opshub.domain.events import ProposalGenerated
    from opshub.projections import proposals as proposals_module

    original_apply = proposals_module.ProposalsProjection.apply

    def _failing_apply(self: object, conn: object, event: object) -> None:
        if isinstance(event, ProposalGenerated):
            raise RuntimeError("simulated projector failure")
        original_apply(self, conn, event)  # type: ignore[arg-type]

    monkeypatch.setattr(
        proposals_module.ProposalsProjection,
        "apply",
        _failing_apply,
    )

    _seed_for_proposal(monkeypatch)

    code, _, _ = _invoke(["propose", "generate", "projector failure topic"])
    # The CLI surfaces the RuntimeError via Typer's default handler;
    # exit code is non-zero, exact value depends on the handler.
    assert code != 0

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.requested")
            ).all()
            generated = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.generated")
            ).all()
            rows = conn.execute(select(proposals_table)).all()
        # Bracket event committed in its own UoW survives.
        assert len(requested) == 1, requested
        # ProposalGenerated rolled back together with the projection
        # apply — neither lands.
        assert len(generated) == 0, generated
        assert rows == [], rows
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. NoOpLLMClient (backend=disabled)
# ---------------------------------------------------------------------------


def test_generate_disabled_backend_exit_2_with_actionable_hint(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[llm] backend = "disabled"`` → exit 2 + setup hint on stderr.

    ADR-0015 §決定 (b) makes ``disabled`` the default. ``opshub propose
    generate`` short-circuits BEFORE constructing the service so no
    proposal event is appended (cheap fast-fail mirroring
    :func:`opshub.cli.brief.brief_command`).
    """
    # Leave OPSHUB_LLM__BACKEND unset — the default is "disabled".
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    code, _, stderr = _invoke(["propose", "generate", "any topic"])
    assert code == 2, stderr
    assert "[llm] backend is disabled" in stderr, stderr
    assert "anthropic" in stderr or "openai" in stderr, stderr

    # No proposal event was appended.
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            proposal_events = conn.execute(
                select(events_table).where(events_table.c.event_type.like("proposal.%"))
            ).all()
        assert proposal_events == [], proposal_events
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 4. Already-applied candidate re-apply
# ---------------------------------------------------------------------------


def test_apply_already_applied_candidate_reraises_no_duplicate_task(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second apply on the same candidate → exit 1 + message + no duplicate.

    ADR-0016 §決定 (d) idempotency guard: the projector itself is
    permissive (replay-safe), but the service raises
    :class:`OpsHubError` so the operator never accidentally re-creates
    the same task.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch, _StubLLMClient())

    _seed_for_proposal(monkeypatch)
    proposal_id = _generate_proposal(monkeypatch)

    code, apply_out, _ = _invoke(["propose", "apply", proposal_id, "0"])
    assert code == 0, apply_out

    code, _, stderr = _invoke(["propose", "apply", proposal_id, "0"])
    assert code == 1, stderr
    assert "candidate 0 already applied" in stderr, stderr

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        # Exactly one TaskCreated event tied to a ProposalApplied; the
        # second apply attempt must not have minted a fresh task.
        with engine.connect() as conn:
            applied_events = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.applied")
            ).all()
            task_created_count = len(
                conn.execute(
                    select(events_table).where(events_table.c.event_type == "task.created")
                ).all()
            )
            states = conn.execute(
                select(proposals_table.c.candidate_states).where(
                    proposals_table.c.id == proposal_id
                )
            ).scalar_one()
        assert len(applied_events) == 1, applied_events
        # The integration seeded one task before generating the
        # proposal; the apply call added a second. The retry must not
        # have added a third.
        assert task_created_count == 2, task_created_count
        assert list(states) == ["applied", "pending"]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. Already-applied candidate reject
# ---------------------------------------------------------------------------


def test_reject_already_applied_candidate_fails_without_event(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject on an already-applied candidate → exit 1, no ProposalRejected.

    Idempotency contract (ADR-0016 §決定 (d)) covers reject as well as
    re-apply: once a candidate has transitioned out of ``pending``, the
    service refuses any further transition.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch, _StubLLMClient())

    _seed_for_proposal(monkeypatch)
    proposal_id = _generate_proposal(monkeypatch)

    code, _, _ = _invoke(["propose", "apply", proposal_id, "0"])
    assert code == 0

    code, _, stderr = _invoke(["propose", "reject", proposal_id, "0"])
    assert code == 1, stderr
    assert "candidate 0 already applied" in stderr, stderr

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            rejected_events = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.rejected")
            ).all()
            states = conn.execute(
                select(proposals_table.c.candidate_states).where(
                    proposals_table.c.id == proposal_id
                )
            ).scalar_one()
        assert rejected_events == [], rejected_events
        assert list(states) == ["applied", "pending"]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 6. Cross-proposal duplicate — current intended behaviour pin (#500)
# ---------------------------------------------------------------------------


def test_cross_proposal_duplicate_apply_mints_two_distinct_tasks(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin: same source → 2 proposals → 2 applies → 2 distinct tasks.

    This pins the **current intended behaviour**, not a bug fix. There
    is no cross-proposal semantic dedup in opshub today:

    - ``ProposalService.generate`` mints a fresh aggregate on every call
      and never inspects other open proposals for the same source.
    - ADR-0016 §決定 (d) idempotency is keyed on
      ``(proposal_id, candidate_index)`` — it stops a *single* candidate
      from being applied twice (pinned by
      :func:`test_apply_already_applied_candidate_reraises_no_duplicate_task`),
      but says nothing across proposals.
    - ``TaskService.create_task`` mints a fresh ULID with no title/body
      dedup.

    Consequence: two concurrent sessions that each
    ``propose generate`` against the same source and then HITL-apply an
    equivalent candidate create two near-identical tasks with distinct
    ULIDs. HITL review (ADR-0016 §決定 (c)) is the only line of defence
    against this — the duplicate is *visible* to the human at the apply
    gate, but nothing blocks it structurally.

    Automated mitigation (e.g. a same-source open-proposal warning at
    generate time) is deliberately out of scope here and tracked in
    follow-up #501. See the investigation on #500:
    https://github.com/ozzy-labs/opshub/issues/500#issuecomment-4642014268

    If a future change adds cross-proposal dedup, this test is expected
    to fail and should be updated alongside #501 — that is the signal,
    not a regression.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    # Both stubs return the same candidate list, modelling two sessions
    # whose LLMs independently emit an equivalent task candidate from the
    # same recalled source.
    _install_stub_llm(monkeypatch, _StubLLMClient())

    _seed_for_proposal(monkeypatch)

    # Two independent generate calls over the same seeded source — no
    # check on the existing open proposal is performed.
    proposal_a = _generate_proposal(monkeypatch, topic="cross-proposal topic A")
    proposal_b = _generate_proposal(monkeypatch, topic="cross-proposal topic B")
    assert proposal_a != proposal_b, (proposal_a, proposal_b)

    # Each session applies its own task candidate (index 0).
    code, apply_a, stderr_a = _invoke(["propose", "apply", proposal_a, "0"])
    assert code == 0, stderr_a or apply_a
    code, apply_b, stderr_b = _invoke(["propose", "apply", proposal_b, "0"])
    assert code == 0, stderr_b or apply_b

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            applied_events = conn.execute(
                select(events_table).where(events_table.c.event_type == "proposal.applied")
            ).all()
            task_created = conn.execute(
                select(events_table).where(events_table.c.event_type == "task.created")
            ).all()
        # One ProposalApplied per proposal, against distinct aggregates.
        assert len(applied_events) == 2, applied_events
        applied_aggregates = {row.aggregate_id for row in applied_events}
        assert applied_aggregates == {proposal_a, proposal_b}, applied_aggregates

        # Seed minted 1 task; each cross-proposal apply minted 1 more →
        # 3 total. Crucially the two proposal-applied tasks are distinct
        # ULIDs: nothing deduplicated the semantically equivalent
        # candidates across the two proposals.
        assert len(task_created) == 3, task_created
        task_ids = {row.aggregate_id for row in task_created}
        assert len(task_ids) == 3, task_ids
    finally:
        engine.dispose()


# Re-export ``pytest`` so static analysers see this module is a pytest test.
_ = pytest
