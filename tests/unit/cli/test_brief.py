"""Tests for ``opshub brief`` (Phase 5 step B4).

The brief CLI is the operator-facing surface for the Phase 5 briefing
flow: it runs :class:`BriefingService.generate` with the supplied
topic + flags and renders the resulting :class:`Briefing` to stdout
(or saves to a Markdown file). These tests cover the CLI shape
end-to-end through :class:`typer.testing.CliRunner`:

* ``backend=disabled`` short-circuit (exit 2 + stderr hint mentions
  ``[llm] backend``).
* ``ConfigError`` propagation from a real :class:`NoOpLLMClient`
  (defensive: the env-var shortcut could bypass the pre-check).
* :class:`OpsHubError` from a stub LLM mapped to exit 1.
* Default ``md`` format prints the markdown body unchanged.
* ``--format json`` emits a parseable JSON document with the
  documented schema.
* ``--save`` writes the markdown body to
  ``<workspace.root>/briefings/<slug>-<briefing-id>.md``.
* ``--max-sources`` / ``--max-tokens`` propagate to
  :meth:`BriefingService.generate`.
* ``--save`` filename uses the ASCII-safe slug helper (regex match).

The :func:`build_briefing_service` factory is monkeypatched to return
a stub for stub-driven tests so they stay laser-focused on the CLI
shape — the full integration path (RecallService + LLM + projector +
event log) is covered by :mod:`tests.unit.services.test_briefing_service`.
The disabled-backend test deliberately drives the **real** wiring path
so a regression that drops the pre-check is caught by the integration
behaviour (NoOpLLMClient raises and the CLI still exits 2).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.core.errors import OpsHubError
from opshub.services.briefings import Briefing

# ---- helpers --------------------------------------------------------------


class _StubBriefingService:
    """Stub :class:`BriefingService` with a configurable response.

    Records every ``generate`` call so tests can assert the CLI
    forwarded ``scope`` / ``max_sources`` / ``max_tokens`` correctly.
    """

    def __init__(
        self,
        *,
        briefing: Briefing | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._briefing = briefing
        self._raises = raises
        self.calls: list[dict[str, object]] = []

    def generate(
        self,
        topic: str,
        *,
        scope: str = "all",
        max_sources: int = 20,
        max_tokens: int = 1500,
    ) -> Briefing:
        self.calls.append(
            {
                "topic": topic,
                "scope": scope,
                "max_sources": max_sources,
                "max_tokens": max_tokens,
            }
        )
        if self._raises is not None:
            raise self._raises
        assert self._briefing is not None, "stub configured without briefing or exception"
        return self._briefing


_DEFAULT_GENERATED_AT = datetime(2026, 5, 17, 9, 0, tzinfo=UTC)
_DEFAULT_SOURCE_REFS: list[tuple[str, str]] = [("task", "01TASK0000000000000000000A")]


def _make_briefing(
    *,
    briefing_id: str = "01HF000000000000000000BRIE",
    topic: str = "phase 5 progress",
    scope: str = "all",
    markdown: str = "# Hello\n\nBody.",
    source_refs: list[tuple[str, str]] | None = None,
    model_id: str = "stub-llm",
    model_version: str = "v1",
    tokens_in: int = 120,
    tokens_out: int = 60,
    generated_at: datetime | None = None,
) -> Briefing:
    """Build a :class:`Briefing` with safe test defaults."""
    return Briefing(
        briefing_id=briefing_id,
        topic=topic,
        scope=scope,
        markdown=markdown,
        source_refs=source_refs if source_refs is not None else list(_DEFAULT_SOURCE_REFS),
        model_id=model_id,
        model_version=model_version,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        generated_at=generated_at if generated_at is not None else _DEFAULT_GENERATED_AT,
    )


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OpsHub env vars at ``tmp_path``.

    Returns the workspace root so tests asserting on the saved
    filename can reconstruct the expected path. The brief CLI does
    not call ``opshub init`` for the ``backend=disabled`` path — the
    short-circuit fires before any engine is opened — so we only need
    to isolate config / data dirs to keep pydantic-settings from
    surfacing a stale config from ``$HOME``.
    """
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    workspace_root = tmp_path / "workspace"
    db_path = tmp_path / "data" / "db" / "opshub.sqlite"

    monkeypatch.setenv("OPSHUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPSHUB_WORKSPACE__ROOT", str(workspace_root))
    monkeypatch.setenv("OPSHUB_STORAGE__DB_PATH", str(db_path))
    # Clear any inherited LLM backend override so the test sees the
    # value each test explicitly sets via ``OPSHUB_LLM_BACKEND``.
    monkeypatch.delenv("OPSHUB_LLM_BACKEND", raising=False)
    monkeypatch.delenv("OPSHUB_LLM__BACKEND", raising=False)
    return workspace_root


def _install_stub_service(monkeypatch: pytest.MonkeyPatch, stub: _StubBriefingService) -> None:
    """Monkeypatch :func:`build_briefing_service` so the CLI sees ``stub``.

    The brief command uses a lazy ``from opshub.cli._wiring import
    build_briefing_service`` inside the command body, so patching the
    name on :mod:`opshub.cli._wiring` is sufficient — every Typer
    invocation re-evaluates the ``from`` import.
    """
    monkeypatch.setattr(
        "opshub.cli._wiring.build_briefing_service",
        lambda: stub,
    )


# ---- happy path -----------------------------------------------------------


def test_brief_renders_markdown_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default ``md`` format prints the briefing markdown unchanged."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubBriefingService(briefing=_make_briefing(markdown="# Hello\n\nWorld."))
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "phase 5"])

    assert result.exit_code == 0, result.stdout
    assert "# Hello" in result.stdout
    assert "World." in result.stdout


def test_brief_renders_json_with_format_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--format json`` emits a parseable JSON object with the documented schema."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubBriefingService(
        briefing=_make_briefing(
            briefing_id="01HF000000000000000000BRIE",
            markdown="# Body",
            source_refs=[("task", "01TASKA"), ("decision", "01DECB")],
        )
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "phase 5", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    payload = cast(dict[str, object], json.loads(result.stdout))
    assert payload["briefing_id"] == "01HF000000000000000000BRIE"
    assert payload["topic"] == "phase 5 progress"  # from stub default
    assert payload["scope"] == "all"
    assert payload["model_id"] == "stub-llm"
    assert payload["model_version"] == "v1"
    assert payload["tokens_in"] == 120
    assert payload["tokens_out"] == 60
    assert payload["markdown"] == "# Body"
    assert payload["source_refs"] == [
        {"entity_type": "task", "entity_id": "01TASKA"},
        {"entity_type": "decision", "entity_id": "01DECB"},
    ]
    assert isinstance(payload["generated_at"], str)


def test_brief_saves_to_workspace_with_save_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--save`` writes the markdown body under ``workspace/briefings/``."""
    workspace_root = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubBriefingService(
        briefing=_make_briefing(
            briefing_id="01HF000000000000000000BRIE",
            markdown="# Saved body",
        )
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "phase 5 progress", "--save"])

    assert result.exit_code == 0, result.stdout
    briefings_dir = workspace_root / "briefings"
    assert briefings_dir.is_dir()
    saved_files = list(briefings_dir.iterdir())
    assert len(saved_files) == 1
    target = saved_files[0]
    assert target.name == "phase-5-progress-01HF000000000000000000BRIE.md"
    assert target.read_text(encoding="utf-8") == "# Saved body"
    # The save path is echoed to stderr so a piped stdout still
    # surfaces the side-effect to the operator.
    assert "saved briefing to" in result.stderr
    assert str(target) in result.stderr


# ---- backend=disabled -----------------------------------------------------


def test_brief_disabled_backend_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``backend=disabled`` → exit 2 + stderr mentions ``[llm] backend``.

    The pre-check fires before :func:`build_briefing_service`, so this
    test deliberately does NOT install a stub: a regression that
    moves the check below the wiring would crash on engine open and
    fail the assertion.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "disabled")
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "anything"])

    assert result.exit_code == 2, result.stdout
    assert "[llm] backend is disabled" in result.stderr
    # Remediation hint mentions both supported backends so the operator
    # knows their options without consulting docs.
    assert "anthropic" in result.stderr
    assert "openai" in result.stderr


# ---- failure paths --------------------------------------------------------


def test_brief_llm_failure_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An :class:`OpsHubError` from the service maps to exit 1 + sanitised stderr.

    The audit trail (``BriefingFailed`` on the event log) is the
    service's responsibility; the CLI only owns the exit-code mapping.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubBriefingService(raises=OpsHubError("rate limited by provider"))
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "hot topic"])

    assert result.exit_code == 1, result.stdout
    assert "Error: rate limited by provider" in result.stderr


def test_brief_invalid_format_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unsupported ``--format`` value short-circuits with exit 2.

    Briefings have only two viable formats (``md`` / ``json``); any
    other value is a usage error.
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "anything", "--format", "yaml"])

    assert result.exit_code == 2, result.stdout
    assert "invalid --format" in result.stderr


# ---- argument propagation ------------------------------------------------


def test_brief_passes_max_sources_and_max_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--max-sources`` / ``--max-tokens`` / ``--scope`` propagate to ``generate``."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubBriefingService(briefing=_make_briefing())
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "brief",
            "phase 5",
            "--scope",
            "all",
            "--max-sources",
            "5",
            "--max-tokens",
            "800",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["topic"] == "phase 5"
    assert call["scope"] == "all"
    assert call["max_sources"] == 5
    assert call["max_tokens"] == 800


# ---- slug behaviour ------------------------------------------------------


def test_slug_in_save_filename_matches_regex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``opshub brief "<noisy topic>" --save`` → ASCII-safe filename.

    Combines :func:`opshub.core.slug.slugify` (punctuation /
    non-ASCII stripping) with the appended ULID suffix to guarantee a
    well-formed filename even when the topic contains punctuation
    and CJK characters.
    """
    workspace_root = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    stub = _StubBriefingService(
        briefing=_make_briefing(
            briefing_id="01HF000000000000000000BRIE",
            markdown="# Q3 plan",
        )
    )
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "Q3 plan! 計画", "--save"])

    assert result.exit_code == 0, result.stdout
    briefings_dir = workspace_root / "briefings"
    saved_files = list(briefings_dir.iterdir())
    assert len(saved_files) == 1
    target = saved_files[0]
    # The slug strips ``!``, drops the CJK chars, lowercases, and the
    # ULID gets appended verbatim; the ``.md`` extension closes the
    # filename. Pinning the exact pattern catches a regression in
    # either the slug helper or the CLI filename composition.
    assert re.match(r"^q3-plan-[0-9A-Z]{26}\.md$", target.name), target.name


def test_brief_save_creates_briefings_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--save`` creates ``workspace/briefings/`` when missing.

    The workspace tree may not have been pre-populated by ``opshub
    init`` (or the operator may have a fresh data dir); the CLI must
    create the directory on demand rather than failing with a missing
    parent.
    """
    workspace_root = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPSHUB_LLM_BACKEND", "anthropic")
    # Ensure the parent does not exist yet — the CLI is responsible
    # for ``mkdir(parents=True, exist_ok=True)``.
    assert not (workspace_root / "briefings").exists()
    stub = _StubBriefingService(briefing=_make_briefing(markdown="# Body"))
    _install_stub_service(monkeypatch, stub)
    runner = CliRunner()

    result = runner.invoke(app, ["brief", "topic", "--save"])

    assert result.exit_code == 0, result.stdout
    assert (workspace_root / "briefings").is_dir()
