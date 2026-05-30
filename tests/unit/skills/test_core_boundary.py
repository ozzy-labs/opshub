"""Boundary tests for the ①core vs ②secretary-layer split (ADR-0004 §(a)).

Phase 10 form-A pins that opshub does **not** hold any agent runtime
code. The five secretary skills live in ``docs/skills/`` as reference
specs (with implementation distributed via ``ozzy-labs/skills``), and
no runtime / always-on / agent-loop module ships under ``src/opshub/``.

These tests catch drift where someone:

* adds an agent ``runtime`` / ``loop`` module under ``src/opshub/``;
* lands a long-running background thread / scheduler in the ① core;
* imports an external agent SDK (``langgraph``, ``crewai``,
  ``autogen``, ``claude-agent-sdk``) into the package.

The checks are static (filesystem + regex over package source). They
do not execute opshub, so they stay fast and safe even on CI hosts
without the optional connectors / LLM extras installed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = _REPO_ROOT / "src" / "opshub"


def _iter_package_files() -> Iterator[Path]:
    yield from _PACKAGE_ROOT.rglob("*.py")


def test_core_has_no_agent_runtime_module() -> None:
    """No ``src/opshub/agent/`` or ``src/opshub/runtime/`` directory.

    ADR-0004 §(a) — opshub does not host an agent runtime.
    """
    forbidden = ("agent", "runtime", "secretary")
    for entry in _PACKAGE_ROOT.iterdir():
        if not entry.is_dir():
            continue
        assert entry.name not in forbidden, (
            f"forbidden ①core module dir found: {entry} — "
            f"agent / runtime / secretary code must not live inside src/opshub/ (ADR-0004 §(a))"
        )


_AGENT_SDK_IMPORTS = (
    re.compile(r"^\s*import\s+langgraph(\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+langgraph(\.|$)", re.MULTILINE),
    re.compile(r"^\s*import\s+crewai(\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+crewai(\.|$)", re.MULTILINE),
    re.compile(r"^\s*import\s+autogen(\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+autogen(\.|$)", re.MULTILINE),
    re.compile(r"^\s*import\s+claude_agent_sdk(\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+claude_agent_sdk(\.|$)", re.MULTILINE),
)


def test_core_does_not_import_external_agent_runtime_sdks() -> None:
    """No external agent runtime SDK is imported from ``src/opshub/``.

    ADR-0004 §(a) / Alternatives §5 — opshub does not embed an agent
    runtime. Importing one would silently re-introduce form-B.
    """
    violations: list[str] = []
    for path in _iter_package_files():
        text = path.read_text(encoding="utf-8")
        for pattern in _AGENT_SDK_IMPORTS:
            match = pattern.search(text)
            if match is not None:
                violations.append(f"{path}:{match.group(0).strip()}")
    assert not violations, (
        "external agent runtime SDK import(s) detected in ①core:\n  " + "\n  ".join(violations)
    )


def test_core_has_no_thread_or_scheduler_daemon_module() -> None:
    """No module under ``src/opshub/`` whose name signals a daemon.

    ADR-0004 §(a) and Phase 10 plan §1 #5 —能動性 (always-on push)
    is not part of Phase 10. A daemon / scheduler / watcher module
    appearing in ①core would silently breach that boundary.
    """
    forbidden_substrings = ("daemon", "scheduler", "always_on", "filewatch", "watcher")
    violations: list[str] = []
    for path in _iter_package_files():
        # Files ending in ``_test.py`` or living under fixtures are
        # not core code; but in ``src/opshub/`` everything is core.
        name = path.name.lower()
        for token in forbidden_substrings:
            if token in name:
                violations.append(str(path))
                break
    assert not violations, (
        "①core module(s) with daemon / scheduler semantics found:\n  "
        + "\n  ".join(violations)
        + "\n(ADR-0004 §(a) — opshub does not host an agent runtime / daemon)"
    )


def test_skill_specs_live_in_docs_not_src() -> None:
    """The five secretary skill SKILL.md files live under ``docs/skills/``.

    ADR-0004 §(c) — skill bodies are distributed via the
    ``ozzy-labs/skills`` preset. Inside this repo only the spec /
    reference lives, and it lives under ``docs/`` so the
    ``uv tool install opshub`` payload does not carry them
    (preserving the M6 cold-start budget, ADR-0006).
    """
    docs_skills = _REPO_ROOT / "docs" / "skills"
    src_skills = _PACKAGE_ROOT / "skills"

    assert docs_skills.is_dir(), "docs/skills/ is the SSOT for secretary skill specs"
    assert not src_skills.exists(), (
        "src/opshub/skills/ must not exist — skill bodies are distributed via "
        "ozzy-labs/skills preset, opshub keeps only the spec under docs/skills/"
    )
