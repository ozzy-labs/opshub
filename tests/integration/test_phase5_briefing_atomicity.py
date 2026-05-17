"""Phase 5 briefing atomicity + failure-path integration tests.

Mirrors the Phase 4 closeout style (one test function per failure mode)
but exercises the briefing-specific contracts pinned by ADR-0015 §決定 (b)
/ (f) / (h) and the BriefingService UoW shape.

What this pins
--------------

- **LLM failure during generate** → exit code 1, ``BriefingRequested``
  durable, ``BriefingFailed`` durable with sanitised message,
  ``briefings`` projection has zero rows (no partial state). Phase 5
  plan §3 Sub-issue B bullet #4.
- **Projector failure during generate** → the same UoW rolls back so
  neither ``BriefingGenerated`` event nor the projection row land.
  Phase 5 plan §1.1 (atomic failing-projector contract).
- **NoOpLLMClient (backend=disabled)** → exit code 2 + actionable
  hint on stderr (``[llm] backend is disabled``), and the disabled
  pre-check in ``opshub brief`` short-circuits before any event is
  appended. Phase 5 plan §3 Sub-issue A bullet #1 + ADR-0015 §決定 (b).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip when sqlite-vec is not installed (matches
# ``test_phase4_lifecycle`` / ``test_phase5_lifecycle``).
pytest.importorskip("sqlite_vec")

from sqlalchemy import select
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.db.engine import create_engine_for_sqlite
from opshub.db.schema import events_table
from opshub.llm.client import LLMMessage, LLMResponse
from opshub.projections.briefings import briefings_table
from opshub.vectors.embedder import EmbeddingResult

_PathsDict = dict[str, Path]


# ---------------------------------------------------------------------------
# Stubs (copied from test_phase5_lifecycle so the two modules stay
# independent — refactoring one must not break the other)
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Deterministic embedder stub (same shape as the lifecycle test)."""

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "phase5-atomicity-embedder"

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


class _FailingLLMClient:
    """LLMClient stub that always raises on ``complete``.

    The raised exception type is ``RuntimeError`` to mimic a generic
    SDK transport failure; the BriefingService records the failure
    on a ``BriefingFailed`` event and re-raises the original
    exception, which the CLI maps to exit code 1.
    """

    def __init__(self, *, message: str = "rate limit exceeded") -> None:
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
        raise RuntimeError(self._message)


class _StubLLMClient:
    """LLMClient stub that returns a fixed response (success path)."""

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
            text="# OK\n",
            model_id=self.model_id,
            model_version=self.model_version,
            tokens_in=10,
            tokens_out=5,
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


# ---------------------------------------------------------------------------
# Test 1: LLM failure during generate
# ---------------------------------------------------------------------------


def test_brief_llm_failure_records_failed_event_no_projection_row(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM raises → exit 1, Requested + Failed events, no projection row.

    Pins the failure semantics from ADR-0015 §決定 (h): the audit row
    (``BriefingFailed``) is durable even when the LLM SDK raises, and
    the read model never sees a partial briefing row.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch, _FailingLLMClient())

    # Seed enough state for the recall path to find at least one hit;
    # the BriefingService still proceeds with an empty source set so
    # this is defensive (and matches how an operator would normally
    # have items in their workspace).
    code, _, _ = _invoke(["task", "create", "failure-mode seed task"])
    assert code == 0
    code, _, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0

    code, _, stderr = _invoke(["brief", "failing topic"])
    assert code == 1, stderr
    # The sanitised error message reaches stderr through
    # ``opshub.cli.brief.brief_command``'s OpsHubError branch — but
    # the original ``RuntimeError`` is a plain Python exception, not
    # an OpsHubError, so CliRunner surfaces it via Typer's default
    # exception handler. We assert the briefing audit trail directly.

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table).where(events_table.c.event_type == "briefing.requested")
            ).all()
            failed = conn.execute(
                select(events_table).where(events_table.c.event_type == "briefing.failed")
            ).all()
            generated = conn.execute(
                select(events_table).where(events_table.c.event_type == "briefing.generated")
            ).all()
            projection_rows = conn.execute(select(briefings_table)).all()
        # Bracket event durable.
        assert len(requested) == 1, requested
        # Failure event durable + same aggregate_id as the request.
        assert len(failed) == 1, failed
        assert requested[0].aggregate_id == failed[0].aggregate_id
        # No success event, no projection row — atomicity holds.
        assert len(generated) == 0, generated
        assert projection_rows == [], projection_rows
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 2: Projector failure during BriefingGenerated apply
# ---------------------------------------------------------------------------


def test_brief_projector_failure_rolls_back_generated_event(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projector apply failure rolls back the BriefingGenerated UoW.

    The :class:`BriefingService` runs ``store.append(BriefingGenerated)`` +
    ``projector.apply(BriefingGenerated)`` inside one UoW (the
    ``engine.begin()`` context manager). If the projector raises, the
    UoW rolls back and neither the event nor the projection row should
    land. We simulate the failure by monkey-patching
    :meth:`opshub.projections.briefings.BriefingsProjection.apply` to
    raise when it sees a ``BriefingGenerated`` event.

    The bracketing ``BriefingRequested`` event uses a separate UoW and
    commits normally, mirroring the real-world rollback semantics:
    the audit trail of "the operator asked for a briefing" survives
    even when the projection-side ends up failing.
    """
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    monkeypatch.setenv("OPSHUB_LLM__BACKEND", "anthropic")
    _install_stub_embedder(monkeypatch)
    _install_stub_llm(monkeypatch, _StubLLMClient())

    # Monkey-patch BriefingsProjection.apply BEFORE the CLI invocation
    # so the wiring sees the failing reducer. We import the class
    # directly (not via ``_wiring``) because ``_wiring`` constructs a
    # fresh instance per CLI invocation — patching the class
    # attribute reaches every instance.
    from opshub.domain.events import BriefingGenerated
    from opshub.projections import briefings as briefings_module

    original_apply = briefings_module.BriefingsProjection.apply

    def _failing_apply(self: object, conn: object, event: object) -> None:
        if isinstance(event, BriefingGenerated):
            raise RuntimeError("simulated projector failure")
        # Other event types (BriefingRequested / BriefingFailed) pass
        # through so the bracket / failure events still commit.
        original_apply(self, conn, event)  # type: ignore[arg-type]

    monkeypatch.setattr(
        briefings_module.BriefingsProjection,
        "apply",
        _failing_apply,
    )

    code, _, _ = _invoke(["task", "create", "projector failure seed task"])
    assert code == 0
    code, _, _ = _invoke(["embeddings", "rebuild"])
    assert code == 0

    code, _, _ = _invoke(["brief", "projector failure topic"])
    # The CLI surfaces the RuntimeError via Typer's default handler;
    # the exact exit code depends on the handler but is non-zero.
    assert code != 0

    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            requested = conn.execute(
                select(events_table).where(events_table.c.event_type == "briefing.requested")
            ).all()
            generated = conn.execute(
                select(events_table).where(events_table.c.event_type == "briefing.generated")
            ).all()
            projection_rows = conn.execute(select(briefings_table)).all()
        # Bracket event commits normally (separate UoW).
        assert len(requested) == 1, requested
        # Generated event rolled back together with the projection
        # apply — neither lands.
        assert len(generated) == 0, generated
        assert projection_rows == [], projection_rows
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 3: NoOpLLMClient (backend=disabled)
# ---------------------------------------------------------------------------


def test_brief_disabled_backend_exit_2_with_actionable_hint(
    isolated_env: _PathsDict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[llm] backend = "disabled"`` → exit 2 + setup hint on stderr.

    ADR-0015 §決定 (b) makes ``disabled`` the Phase 5 default so a
    fresh ``uv tool install`` never silently bills the operator. The
    CLI short-circuits BEFORE constructing the briefing service so
    no event is appended (cheap fast-fail).
    """
    # Leave OPSHUB_LLM__BACKEND unset — the default is "disabled".
    # We still need the embedding backend so the wiring succeeds up
    # to the disabled-backend check (which happens early in
    # ``brief_command``, BEFORE the briefing service is built).
    monkeypatch.setenv("OPSHUB_EMBEDDING__BACKEND", "local")
    _install_stub_embedder(monkeypatch)

    code, _, stderr = _invoke(["brief", "any topic"])
    assert code == 2, stderr
    # The hint must reference the backend config so an operator
    # running ``opshub brief`` straight after install knows what to
    # change. Pin the load-bearing substring; the full wording can
    # evolve.
    assert "[llm] backend is disabled" in stderr, stderr
    assert "anthropic" in stderr or "openai" in stderr, stderr

    # No event was appended — the disabled-backend check
    # short-circuits before BriefingService is constructed.
    engine = create_engine_for_sqlite(isolated_env["db_path"])
    try:
        with engine.connect() as conn:
            briefing_events = conn.execute(
                select(events_table).where(events_table.c.event_type.like("briefing.%"))
            ).all()
        assert briefing_events == [], briefing_events
    finally:
        engine.dispose()


# Re-export ``pytest`` so static analysers see this module is a pytest test.
_ = pytest
