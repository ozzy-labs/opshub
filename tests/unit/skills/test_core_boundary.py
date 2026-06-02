"""Boundary tests for the ①core vs ②secretary-layer split (ADR-0004 §(a)).

Phase 10 form-A pins that opshub does **not** hold any agent runtime
code. The 14 secretary skills live in ``docs/skills/`` as the SSOT
([ADR-0004 §決定 (c)](docs/adr/0004-agent-runtime-boundary.md), Phase
12 H1). Phase 16-A ([ADR-0029](docs/adr/0029-distribute-secretary-skills-via-opshub-package.md))
revised the distribution channel: the 14 secretary skills are bundled
into the opshub Python package (`[tool.hatch.build.force-include]`
copies ``docs/skills`` → ``src/opshub/_skills`` at build time) and
shipped via ``opshub skills install``. SSOT location is unchanged;
only the distribution path moved (from ``ozzy-labs/skills`` Renovate
preset to opshub package bundling). No runtime / always-on / agent-loop
module ships under ``src/opshub/``.

These tests catch drift where someone:

* adds an agent ``runtime`` / ``loop`` module under ``src/opshub/``;
* lands a long-running background thread / scheduler in the ① core;
* imports an external agent SDK (``langgraph``, ``crewai``,
  ``autogen``, ``claude-agent-sdk``) into the package;
* ships executable Python under the ``src/opshub/_skills/`` bundle
  payload (the payload must stay declarative SKILL.md + reference/
  text only, ADR-0029 §不変条件 1).

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
    """The 14 secretary skill SKILL.md files live under ``docs/skills/``.

    ADR-0004 §(c) (Phase 12 H1, revised by Phase 16-A) — ``docs/skills/``
    is the SSOT for the 14 secretary skill specs. Phase 16-A
    ([ADR-0029](docs/adr/0029-distribute-secretary-skills-via-opshub-package.md))
    introduces a build-time copy from ``docs/skills/`` to
    ``src/opshub/_skills/`` for package bundling — that bundle path
    is checked by ``test_skills_payload_contains_no_python`` below.

    A directly-authored ``src/opshub/skills/`` directory (no leading
    underscore) is still forbidden: hand-edited skills under the
    package root would re-introduce a runtime / mutable skill module
    surface and bypass the build-time SSOT → bundle copy.
    """
    docs_skills = _REPO_ROOT / "docs" / "skills"
    src_skills = _PACKAGE_ROOT / "skills"

    assert docs_skills.is_dir(), "docs/skills/ is the SSOT for secretary skill specs"
    assert not src_skills.exists(), (
        "src/opshub/skills/ must not exist — secretary skill SSOT lives under "
        "docs/skills/ and is bundled into src/opshub/_skills/ at build time "
        "(ADR-0029 §決定 (a))."
    )


def test_skills_payload_contains_no_python() -> None:
    """The ``src/opshub/_skills/`` bundle payload is declarative — no ``.py``.

    ADR-0029 §不変条件 1 — the package bundle path
    (``src/opshub/_skills/``, populated by
    ``[tool.hatch.build.force-include]`` at build time from
    ``docs/skills/``) must stay declarative: SKILL.md frontmatter +
    reference Markdown only. Shipping executable Python under
    ``_skills/`` would re-introduce a runtime / agent-loop module via
    the back door (ADR-0004 §(a) form-A boundary).

    The Phase 16-A landing is doc-only; the actual build-time copy
    arrives in Phase 16-B (#383). Until then ``_skills/`` does not
    exist in the source tree, and this assertion **skips** so the
    invariant is pinned but the test is not falsely failing during the
    interim. Once Phase 16-B lands the directory will exist and the
    ``.py`` non-presence becomes an active CI guard.
    """
    skills_payload = _PACKAGE_ROOT / "_skills"
    if not skills_payload.exists():
        # Phase 16-B (#383) populates this directory. Until then the
        # invariant is dormant — explicitly skip so a future contributor
        # adding `_skills/` cannot accidentally bypass the guard.
        return

    py_files = sorted(skills_payload.rglob("*.py"))
    assert not py_files, (
        "src/opshub/_skills/ must stay declarative (SKILL.md + reference/*.md only); "
        "the following Python file(s) were detected:\n  "
        + "\n  ".join(str(p) for p in py_files)
        + "\n(ADR-0029 §不変条件 1 — skills bundle is a declarative payload)"
    )
