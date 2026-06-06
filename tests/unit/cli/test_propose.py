"""Tests for ``opshub propose`` (Phase 6 step B4, ADR-0016).

The propose CLI is the operator-facing surface for the Phase 6 Action
loop: four subcommands (``generate`` / ``list`` / ``apply`` /
``reject``) wired against :class:`ProposalService`. These tests cover
the CLI shape end-to-end through :class:`typer.testing.CliRunner`:

* ``generate`` default ``md`` format + ``--format json`` + argument
  propagation (``--max-candidates`` / ``--max-tokens`` /
  ``--from-briefing``) + disabled-backend exit 2 + OpsHubError exit 1.
* ``list`` markdown table + state filter + JSON format.
* ``apply`` happy path + already-applied error + missing proposal.
* ``reject`` happy path + ``--reason`` propagation.

The stub service is monkeypatched onto :mod:`opshub.cli._wiring` for
the lazy ``from`` import inside the command body to pick up. The
``list`` tests seed real rows into the ``proposals`` projection
through a migrated SQLite engine because the list path queries the
projection directly (no service stub).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.errors import OpsHubError
from opshub.db.engine import create_engine_for_sqlite
from opshub.domain.events.proposal import (
    DecisionCandidatePayload,
    TaskCandidatePayload,
)
from opshub.projections.proposals import proposals_table
from opshub.services.proposals import Proposal

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_LOCATION = _REPO_ROOT / "src" / "opshub" / "db" / "migrations"


# ---- helpers --------------------------------------------------------------


class _StubProposalService:
    """Stub :class:`ProposalService` with configurable responses.

    Records every call so tests can assert the CLI forwarded args
    correctly. The three operations (``generate`` / ``apply`` /
    ``reject``) each have their own ``calls`` accumulator and
    optional pre-baked response / exception.
    """

    def __init__(
        self,
        *,
        proposal: Proposal | None = None,
        reply_draft_proposal: Proposal | None = None,
        generate_raises: Exception | None = None,
        reply_draft_raises: Exception | None = None,
        apply_result: tuple[str, str] = ("task", "01J6APPLIED0000000000000000"),
        apply_raises: Exception | None = None,
        reject_raises: Exception | None = None,
    ) -> None:
        self._proposal = proposal
        self._reply_draft_proposal = reply_draft_proposal
        self._generate_raises = generate_raises
        self._reply_draft_raises = reply_draft_raises
        self._apply_result = apply_result
        self._apply_raises = apply_raises
        self._reject_raises = reject_raises
        self.generate_calls: list[dict[str, object]] = []
        self.generate_reply_draft_calls: list[dict[str, object]] = []
        self.apply_calls: list[tuple[str, int]] = []
        self.reject_calls: list[dict[str, object]] = []

    def generate(
        self,
        topic: str,
        *,
        scope: str = "all",
        from_briefing_id: str | None = None,
        max_candidates: int = 5,
        max_tokens: int = 2000,
    ) -> Proposal:
        self.generate_calls.append(
            {
                "topic": topic,
                "scope": scope,
                "from_briefing_id": from_briefing_id,
                "max_candidates": max_candidates,
                "max_tokens": max_tokens,
            }
        )
        if self._generate_raises is not None:
            raise self._generate_raises
        assert self._proposal is not None, "stub configured without proposal or exception"
        return self._proposal

    def generate_reply_draft(
        self,
        reply_to_source_id: str,
        *,
        max_candidates: int = 3,
        max_tokens: int = 2000,
    ) -> Proposal:
        """Stub the Phase 10 reply-draft service path.

        Records the forwarded args so the ``--reply-to`` CLI tests can
        assert routing happens at exactly the right boundary (no topic
        / from_briefing passed through, only the reply-draft-shaped
        kwargs the service expects).
        """
        self.generate_reply_draft_calls.append(
            {
                "reply_to_source_id": reply_to_source_id,
                "max_candidates": max_candidates,
                "max_tokens": max_tokens,
            }
        )
        if self._reply_draft_raises is not None:
            raise self._reply_draft_raises
        if self._reply_draft_proposal is not None:
            return self._reply_draft_proposal
        assert self._proposal is not None, (
            "stub configured without proposal / reply_draft_proposal or exception"
        )
        return self._proposal

    def apply(self, proposal_id: str, candidate_index: int) -> tuple[str, str]:
        self.apply_calls.append((proposal_id, candidate_index))
        if self._apply_raises is not None:
            raise self._apply_raises
        return self._apply_result

    def reject(
        self,
        proposal_id: str,
        candidate_index: int,
        reason: str | None = None,
    ) -> None:
        self.reject_calls.append(
            {
                "proposal_id": proposal_id,
                "candidate_index": candidate_index,
                "reason": reason,
            }
        )
        if self._reject_raises is not None:
            raise self._reject_raises


_DEFAULT_GENERATED_AT = datetime(2026, 5, 17, 9, 0, tzinfo=UTC)


def _make_proposal(
    *,
    proposal_id: str = "01J6PRO000000000000000001",
    topic: str = "phase 6 next steps",
    scope: str = "all",
    briefing_id: str | None = None,
    candidates: list[TaskCandidatePayload | DecisionCandidatePayload] | None = None,
    model_id: str = "stub-llm",
    model_version: str = "v1",
    tokens_in: int = 850,
    tokens_out: int = 220,
    generated_at: datetime | None = None,
) -> Proposal:
    """Build a :class:`Proposal` with safe test defaults."""
    if candidates is None:
        candidates = [
            TaskCandidatePayload(title="Add prompt versioning", body="Schema migration adds cols."),
            DecisionCandidatePayload(
                text="Defer multi-machine sync",
                context="Phase 7 priority instead.",
            ),
        ]
    return Proposal(
        proposal_id=proposal_id,
        topic=topic,
        scope=scope,
        briefing_id=briefing_id,
        candidates=list(candidates),
        model_id=model_id,
        model_version=model_version,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        generated_at=generated_at if generated_at is not None else _DEFAULT_GENERATED_AT,
    )


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OpsHub env vars at ``tmp_path``.

    Returns the database path so tests that need to run migrations
    (``list`` path) can hand the same path to Alembic and the CLI.
    Clears any inherited ``OPSHUB_LLM_BACKEND`` override so each test
    can set its own value explicitly.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    monkeypatch.delenv("OPSHUB_LLM__BACKEND", raising=False)
    return db_path


def _install_stub_service(monkeypatch: pytest.MonkeyPatch, stub: _StubProposalService) -> None:
    """Monkeypatch :func:`build_proposal_service` so the CLI sees ``stub``.

    The propose commands use a lazy ``from opshub.cli._wiring import
    build_proposal_service`` inside each command body, so patching the
    name on :mod:`opshub.cli._wiring` is sufficient — every Typer
    invocation re-evaluates the ``from`` import.
    """
    monkeypatch.setattr(
        "opshub.cli._wiring.build_proposal_service",
        lambda actor="cli:propose": stub,
    )


def _migrate_db(db_path: Path) -> None:
    """Apply Alembic migrations to ``db_path``.

    Used by the ``opshub propose list`` tests which seed rows into the
    ``proposals`` projection and then invoke the CLI through the real
    :func:`build_engine` path (the ``_require_initialised`` guard
    rejects an un-migrated DB).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


def _seed_proposal_row(
    db_path: Path,
    *,
    proposal_id: str,
    topic: str = "topic",
    scope: str = "all",
    candidate_states: list[str] | None = None,
    generated_at: datetime | None = None,
) -> None:
    """Insert one row into the ``proposals`` projection table.

    The list CLI only reads ``id`` / ``topic`` / ``candidate_states``
    / ``generated_at``, so the other columns get minimal but
    schema-valid defaults. Two task candidates suffice for any
    state-filter test that needs at least two candidates per row.
    """
    states = candidate_states if candidate_states is not None else ["pending", "pending"]
    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(proposals_table).values(
                    id=proposal_id,
                    topic=topic,
                    scope=scope,
                    briefing_id=None,
                    candidates=[
                        {"kind": "task", "schema_version": "v1", "title": "t1", "body": None},
                        {"kind": "task", "schema_version": "v1", "title": "t2", "body": None},
                    ],
                    candidate_states=states,
                    model_id="stub",
                    model_version="v1",
                    tokens_in=10,
                    tokens_out=20,
                    generated_at=generated_at
                    if generated_at is not None
                    else _DEFAULT_GENERATED_AT,
                )
            )
    finally:
        engine.dispose()


# ---- generate -------------------------------------------------------------


def test_generate_renders_markdown_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default ``md`` format renders the proposal as a candidate list."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "generate", "phase 6 next steps"])

    assert result.exit_code == 0, result.stdout
    assert '# Proposals for "phase 6 next steps"' in result.stdout
    assert "Proposal: 01J6PRO000000000000000001" in result.stdout
    assert "[0] task: Add prompt versioning" in result.stdout
    assert "[1] decision: Defer multi-machine sync" in result.stdout
    assert "To apply:" in result.stdout
    assert "To reject:" in result.stdout


def test_generate_renders_json_with_format_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--format json`` emits a parseable JSON object with the documented schema."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "generate", "phase 6", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    payload = cast(dict[str, Any], json.loads(result.stdout))
    assert payload["proposal_id"] == "01J6PRO000000000000000001"
    assert payload["topic"] == "phase 6 next steps"
    assert payload["scope"] == "all"
    assert payload["model_id"] == "stub-llm"
    assert payload["model_version"] == "v1"
    assert payload["tokens_in"] == 850
    assert payload["tokens_out"] == 220
    candidates = cast(list[dict[str, Any]], payload["candidates"])
    assert len(candidates) == 2
    assert candidates[0]["kind"] == "task"
    assert candidates[0]["title"] == "Add prompt versioning"
    assert candidates[1]["kind"] == "decision"
    assert candidates[1]["text"] == "Defer multi-machine sync"


def test_generate_passes_max_candidates_and_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--max-candidates`` / ``--max-tokens`` / ``--scope`` propagate to the service."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "propose",
            "generate",
            "phase 6",
            "--scope",
            "all",
            "--max-candidates",
            "3",
            "--max-tokens",
            "800",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(stub.generate_calls) == 1
    call = stub.generate_calls[0]
    assert call["topic"] == "phase 6"
    assert call["scope"] == "all"
    assert call["max_candidates"] == 3
    assert call["max_tokens"] == 800
    assert call["from_briefing_id"] is None


def test_generate_passes_from_briefing_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--from-briefing <ULID>`` reaches the service as ``from_briefing_id``."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(
        proposal=_make_proposal(briefing_id="01HF000000000000000000BRIE"),
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "propose",
            "generate",
            "topic",
            "--from-briefing",
            "01HF000000000000000000BRIE",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert stub.generate_calls[0]["from_briefing_id"] == "01HF000000000000000000BRIE"


def test_generate_rejects_legacy_expand_graph_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--expand-graph`` was dropped in epic #470; Typer rejects it as unknown option."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["propose", "generate", "phase 8", "--expand-graph"],
    )

    # Typer / Click maps unknown options to exit code 2.
    assert result.exit_code == 2
    # No service call should have happened — argparse rejected early.
    assert stub.generate_calls == []


def test_generate_disabled_backend_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``[llm] backend = "disabled"`` → exit 2 + stderr mentions ``[llm] backend``.

    The pre-check fires before :func:`build_proposal_service`, so this
    test deliberately does NOT install a stub: a regression that
    drops the check would crash on engine open and fail the assertion.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "disabled")
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "generate", "anything"])

    assert result.exit_code == 2, result.stdout
    assert "[llm] backend is disabled" in result.stderr
    assert "anthropic" in result.stderr
    assert "openai" in result.stderr


def test_generate_llm_failure_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An :class:`OpsHubError` from the service maps to exit 1 + sanitised stderr."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(generate_raises=OpsHubError("rate limited by provider"))
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "generate", "hot topic"])

    assert result.exit_code == 1, result.stdout
    assert "Error: rate limited by provider" in result.stderr


def test_generate_invalid_format_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unsupported ``--format`` value short-circuits with exit 2."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "generate", "anything", "--format", "yaml"])

    assert result.exit_code == 2, result.stdout
    assert "invalid --format" in result.stderr


# ---- list -----------------------------------------------------------------


def test_list_renders_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Seed two proposals and assert both rows surface in the markdown view."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    _migrate_db(db_path)
    _seed_proposal_row(
        db_path,
        proposal_id="01J6PRO000000000000000ONE1",
        topic="alpha topic",
        candidate_states=["pending", "pending"],
        generated_at=datetime(2026, 5, 17, 9, 0, tzinfo=UTC),
    )
    _seed_proposal_row(
        db_path,
        proposal_id="01J6PRO000000000000000TWO2",
        topic="bravo topic",
        candidate_states=["applied", "rejected"],
        generated_at=datetime(2026, 5, 17, 10, 0, tzinfo=UTC),
    )
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "list"])

    assert result.exit_code == 0, result.stdout
    assert "alpha topic" in result.stdout
    assert "bravo topic" in result.stdout
    # State breakdown columns: alpha row has 2 pending, bravo has 1 applied + 1 rejected.
    assert "2p/0a/0r" in result.stdout
    assert "0p/1a/1r" in result.stdout


def test_list_filters_by_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--state pending`` excludes proposals with no pending candidates."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    _migrate_db(db_path)
    _seed_proposal_row(
        db_path,
        proposal_id="01J6PRO000000000000000ONE1",
        topic="pending topic",
        candidate_states=["pending", "pending"],
    )
    _seed_proposal_row(
        db_path,
        proposal_id="01J6PRO000000000000000TWO2",
        topic="fully closed",
        candidate_states=["applied", "rejected"],
    )
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "list", "--state", "pending"])

    assert result.exit_code == 0, result.stdout
    assert "pending topic" in result.stdout
    assert "fully closed" not in result.stdout


def test_list_invalid_state_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unknown ``--state`` value → exit 2 with a helpful message."""
    _isolate_env(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "list", "--state", "bogus"])

    assert result.exit_code == 2, result.stdout
    assert "invalid --state" in result.stderr


def test_list_json_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--format json`` returns an array of summary objects."""
    db_path = _isolate_env(monkeypatch, tmp_path)
    _migrate_db(db_path)
    _seed_proposal_row(
        db_path,
        proposal_id="01J6PRO000000000000000ONE1",
        topic="alpha",
        candidate_states=["pending", "applied"],
    )
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "list", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    payload = cast(list[dict[str, Any]], json.loads(result.stdout))
    assert len(payload) == 1
    row = payload[0]
    assert row["proposal_id"] == "01J6PRO000000000000000ONE1"
    assert row["topic"] == "alpha"
    assert row["candidate_count"] == 2
    assert row["states"] == {"pending": 1, "applied": 1, "rejected": 0}


# ---- apply ----------------------------------------------------------------


def test_apply_returns_zero_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Successful apply prints the created entity id and exits 0."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubProposalService(apply_result=("task", "01J6TASK000000000000000001"))
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "apply", "01J6PRO000000000000000001", "0"])

    assert result.exit_code == 0, result.stdout
    assert "Applied candidate [0]" in result.stdout
    assert "task: 01J6TASK000000000000000001" in result.stdout
    assert stub.apply_calls == [("01J6PRO000000000000000001", 0)]


def test_apply_already_applied_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An :class:`OpsHubError` from the service maps to exit 1."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubProposalService(
        apply_raises=OpsHubError("candidate 0 already applied"),
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "apply", "01J6PRO000000000000000001", "0"])

    assert result.exit_code == 1, result.stdout
    assert "Error: candidate 0 already applied" in result.stderr


def test_apply_missing_proposal_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unknown proposal id → exit 1 with the service-supplied message."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubProposalService(
        apply_raises=OpsHubError("proposal 01J6MISSING000000000000000 not found"),
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "apply", "01J6MISSING000000000000000", "0"])

    assert result.exit_code == 1, result.stdout
    assert "Error: proposal 01J6MISSING000000000000000 not found" in result.stderr


# ---- reject ---------------------------------------------------------------


def test_reject_returns_zero_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Successful reject prints a confirmation and exits 0."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubProposalService()
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "reject", "01J6PRO000000000000000001", "1"])

    assert result.exit_code == 0, result.stdout
    assert "Rejected candidate [1]" in result.stdout
    assert len(stub.reject_calls) == 1
    assert stub.reject_calls[0]["proposal_id"] == "01J6PRO000000000000000001"
    assert stub.reject_calls[0]["candidate_index"] == 1
    assert stub.reject_calls[0]["reason"] is None


def test_reject_passes_reason_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--reason "..."`` propagates to :meth:`ProposalService.reject`."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubProposalService()
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "propose",
            "reject",
            "01J6PRO000000000000000001",
            "0",
            "--reason",
            "not actionable",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert stub.reject_calls[0]["reason"] == "not actionable"


def test_reject_failure_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An :class:`OpsHubError` from the service maps to exit 1."""
    _isolate_env(monkeypatch, tmp_path)
    stub = _StubProposalService(
        reject_raises=OpsHubError("candidate 0 already rejected"),
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["propose", "reject", "01J6PRO000000000000000001", "0"])

    assert result.exit_code == 1, result.stdout
    assert "Error: candidate 0 already rejected" in result.stderr


# ---- ADR-0016 §決定 (c) human-in-the-loop ---------------------------------


def test_propose_app_has_no_auto_apply_flag_or_command() -> None:
    """ADR-0016 §決定 (c) human-in-the-loop: proposal apply MUST be operator-triggered.

    The contract is held by **surface absence** — no ``--auto-apply``
    CLI flag, no ``auto_apply`` sub-command. This test is a negative
    pin: if a future refactor accidentally adds either, this test
    fails loudly so the change triggers an explicit ADR-0016 §決定 (c)
    re-evaluation (which per ADR-0016 §決定 (c) requires a new ADR
    that supersedes 0016).

    The walk descends through every Typer command in the ``propose``
    sub-app and inspects every Click option / argument exposed
    underneath. Variants of the forbidden token (``--auto-apply`` /
    ``auto_apply`` / ``--auto_apply`` / ``autoapply``) are all
    rejected so a near-miss spelling still trips the assertion.
    """
    from typer.main import get_command

    from opshub.cli.propose import propose_app

    click_command = get_command(propose_app)

    # Recursively collect every option / argument name exposed under
    # the propose CLI tree. ``param.opts`` is the list of long/short
    # option strings (e.g. ``["--max-candidates"]``); ``param.name`` is
    # the snake-case Python identifier the callback receives. Click's
    # API is loosely typed at the Python level, so we use ``Any``
    # locally and rely on the runtime assertions to pin the surface.
    all_option_names: list[str] = []

    def _walk(cmd: Any) -> None:  # walking Click's untyped tree
        params: list[Any] = list(getattr(cmd, "params", None) or [])
        for param in params:
            opts: list[Any] = list(getattr(param, "opts", None) or [])
            for opt in opts:
                if isinstance(opt, str):
                    all_option_names.append(opt)
            name = getattr(param, "name", None)
            if isinstance(name, str):
                all_option_names.append(name)
        commands = getattr(cmd, "commands", None)
        if isinstance(commands, dict):
            for subcmd in cast(dict[str, Any], commands).values():
                _walk(subcmd)

    _walk(click_command)

    forbidden_lowered = {"--auto-apply", "auto_apply", "--auto_apply", "autoapply"}
    found = [name for name in all_option_names if name.lower() in forbidden_lowered]
    assert not found, (
        f"Forbidden flag / option found in `opshub propose`: {found}. "
        "ADR-0016 §決定 (c) forbids auto-apply — re-evaluation requires "
        "a new ADR that supersedes 0016."
    )

    # Also verify no sub-command of ``propose`` is named ``auto-apply`` /
    # ``auto_apply``. The Typer surface registers four verbs (``generate``
    # / ``list`` / ``apply`` / ``reject``); a future fifth ``auto-apply``
    # verb would breach §決定 (c) even without a flag.
    sub_commands: list[str] = []

    def _collect_command_names(cmd: Any) -> None:
        commands = getattr(cmd, "commands", None)
        if isinstance(commands, dict):
            for sub_name, subcmd in cast(dict[str, Any], commands).items():
                # Click guarantees command dict keys are ``str`` at
                # runtime; coerce explicitly so pyright doesn't flag
                # the redundant isinstance check.
                sub_commands.append(str(sub_name))
                _collect_command_names(subcmd)

    _collect_command_names(click_command)
    forbidden_cmd_names = {"auto-apply", "auto_apply", "autoapply"}
    found_cmds = [name for name in sub_commands if name.lower() in forbidden_cmd_names]
    assert not found_cmds, (
        f"Forbidden sub-command found in `opshub propose`: {found_cmds}. "
        "ADR-0016 §決定 (c) forbids auto-apply — re-evaluation requires "
        "a new ADR that supersedes 0016."
    )


def test_opshub_settings_has_no_auto_apply_field() -> None:
    """ADR-0016 §決定 (c): no ``auto_apply`` field anywhere in the config schema.

    Mirrors :func:`test_propose_app_has_no_auto_apply_flag_or_command`
    on the config surface. Walks every ``BaseModel`` reachable from
    :class:`OpsHubSettings` and asserts no field with an
    ``auto_apply``-shaped name is registered. The §決定 (c) contract is
    "no surface to enable auto-apply, even by config" — a config-only
    backdoor would breach the principle even without a CLI flag.
    """
    from pydantic import BaseModel

    from opshub.core.config import OpsHubSettings

    forbidden_field_names = {"auto_apply", "autoapply"}
    visited: set[type[BaseModel]] = set()
    leaks: list[str] = []

    def _walk_model(model: type[BaseModel], path: str) -> None:
        if model in visited:
            return
        visited.add(model)
        for field_name, field_info in model.model_fields.items():
            qualified = f"{path}.{field_name}" if path else field_name
            if field_name.lower() in forbidden_field_names:
                leaks.append(qualified)
            annotation = field_info.annotation
            # Recurse into nested ``BaseModel`` annotations so a future
            # ``LLMSettings.auto_apply: bool`` or
            # ``OpsHubSettings.propose.auto_apply: bool`` is caught.
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _walk_model(annotation, qualified)

    _walk_model(OpsHubSettings, "")
    assert not leaks, (
        f"Forbidden ``auto_apply`` field found in OpsHubSettings: {leaks}. "
        "ADR-0016 §決定 (c) forbids auto-apply — re-evaluation requires "
        "a new ADR that supersedes 0016."
    )


# ---- generate --reply-to (Phase 10 step E2, ADR-0016 §決定 (i)) -----------


_REPLY_TO_SRC_ID = "01J6SRC0000000000000000001"


def test_generate_reply_to_routes_to_generate_reply_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--reply-to <id>`` dispatches to ``ProposalService.generate_reply_draft``.

    Pins the CLI's reply-draft mode switch (Phase 10 step E2): when
    ``--reply-to`` is supplied the CLI must route to the dedicated
    service method, not to the generic :meth:`ProposalService.generate`
    that would mis-interpret the topic argument.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(reply_draft_proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["propose", "generate", "topic ignored", "--reply-to", _REPLY_TO_SRC_ID],
    )

    assert result.exit_code == 0, result.stdout
    assert stub.generate_calls == []
    assert len(stub.generate_reply_draft_calls) == 1
    call = stub.generate_reply_draft_calls[0]
    assert call["reply_to_source_id"] == _REPLY_TO_SRC_ID


def test_generate_reply_to_ignores_topic_and_from_briefing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In reply-draft mode the ``topic`` argument and ``--from-briefing`` are dropped.

    The CLI declares ``topic`` as a required positional but documents
    that it is ignored when ``--reply-to`` is supplied (Phase 10 step
    E2). ``--from-briefing`` is similarly inert — reply-draft has its
    own context loading (Sub-issue E2 style-example recall +
    ``--expand-graph``).

    Pin: the stub's ``generate_reply_draft`` signature has no
    ``topic`` / ``from_briefing_id`` parameter, so the CLI must NOT
    forward either, and ``stub.generate_calls`` must remain empty
    (else the CLI dual-routed).
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(reply_draft_proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "propose",
            "generate",
            "topic ignored when reply-to is set",
            "--reply-to",
            _REPLY_TO_SRC_ID,
            "--from-briefing",
            "01HF000000000000000000BRIE",
        ],
    )

    assert result.exit_code == 0, result.stdout
    # ``generate`` (the topic-based path) was NOT called — even though
    # ``topic`` and ``--from-briefing`` were both supplied.
    assert stub.generate_calls == []
    assert len(stub.generate_reply_draft_calls) == 1
    call = stub.generate_reply_draft_calls[0]
    assert call["reply_to_source_id"] == _REPLY_TO_SRC_ID
    # No leakage of topic / from_briefing into the reply-draft kwargs:
    # the recorded keys are exactly the three reply-draft inputs.
    assert set(call.keys()) == {
        "reply_to_source_id",
        "max_candidates",
        "max_tokens",
    }


def test_generate_reply_to_rejects_legacy_expand_graph_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--expand-graph`` is also rejected on the reply-draft code path."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(reply_draft_proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "propose",
            "generate",
            "ignored",
            "--reply-to",
            _REPLY_TO_SRC_ID,
            "--expand-graph",
        ],
    )

    assert result.exit_code == 2
    assert stub.generate_reply_draft_calls == []


def test_generate_reply_to_propagates_max_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--max-candidates`` and ``--max-tokens`` propagate in reply-draft mode."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubProposalService(reply_draft_proposal=_make_proposal())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "propose",
            "generate",
            "ignored",
            "--reply-to",
            _REPLY_TO_SRC_ID,
            "--max-candidates",
            "2",
            "--max-tokens",
            "1200",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(stub.generate_reply_draft_calls) == 1
    call = stub.generate_reply_draft_calls[0]
    assert call["max_candidates"] == 2
    assert call["max_tokens"] == 1200
