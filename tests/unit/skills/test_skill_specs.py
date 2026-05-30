"""Format / boundary tests for secretary skill specs under ``docs/skills/``.

The skills live in ``docs/skills/<name>/SKILL.md`` as the reference
spec for the ``ozzy-labs/skills`` distribution (ADR-0004 §(c)).
These tests pin five invariants the Phase 10 plan §3-D / §4-D DoD
requires:

1. All five secretary skills exist (daily-brief / next-actions /
   reply-draft / pr-review / file-lookup).
2. Each file is a valid Anthropic SKILL.md — leading YAML frontmatter
   with ``name`` and ``description`` strings.
3. The body fits inside the 5k-token budget Phase 10 plan §3-D pins
   (we approximate with a 5000-word ceiling — well above 5k tokens —
   to give skill authors headroom while still catching runaway
   bodies; the real budget will be enforced by the host's skill
   loader at activation time).
4. The skill body refers only to MCP tools and CLI commands, never
   to Python module imports of ``opshub.*`` — this is the ②→①
   boundary (skills go through MCP, not direct imports). The check
   is heuristic but catches the common drift.
5. Each spec is clean under the ``tools.skill_scan`` security scan
   (no prompt injection / command injection / hard-coded secrets /
   data exfiltration / frontmatter hidden Unicode / instruction
   overrides).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tools.skill_scan import parse_frontmatter, scan_skill_file

# The five secretary skills committed in Phase 10 Sub-issue D.
_REQUIRED_SKILLS: tuple[str, ...] = (
    "daily-brief",
    "next-actions",
    "reply-draft",
    "pr-review",
    "file-lookup",
)

# Repo-root-relative path to the catalog of spec files. We climb up
# from this test file because pytest may be invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_DIR = _REPO_ROOT / "docs" / "skills"

# A rough ceiling: 5000 words is well over the "≤5k tokens" target
# (English text averages ~0.75 word per token), so any breach signals
# either a runaway body or the author missed the progressive-disclosure
# pattern (L3 = references/, not inlined).
_BODY_WORD_CEILING = 5000


# ---------------------------------------------------------------------------
# 1. Presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _REQUIRED_SKILLS)
def test_skill_spec_exists(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    assert path.is_file(), f"missing secretary skill spec: {path}"


# ---------------------------------------------------------------------------
# 2. SKILL.md format (frontmatter)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _REQUIRED_SKILLS)
def test_skill_frontmatter_has_name_and_description(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)

    assert frontmatter.get("name") == name, (
        f"{path} frontmatter ``name`` must equal directory name; got {frontmatter.get('name')!r}"
    )
    assert frontmatter.get("description"), (
        f"{path} frontmatter must include a non-empty ``description``"
    )
    assert body.strip(), f"{path} must have a non-empty body"


# ---------------------------------------------------------------------------
# 3. Body length budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _REQUIRED_SKILLS)
def test_skill_body_within_budget(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    words = body.split()
    assert len(words) <= _BODY_WORD_CEILING, (
        f"{path} body has {len(words)} words; "
        f"≤{_BODY_WORD_CEILING} expected (Phase 10 plan §3-D 5k tokens budget)"
    )


# ---------------------------------------------------------------------------
# 4. Boundary: skill bodies reference MCP / CLI only, never `import opshub.`
# ---------------------------------------------------------------------------


# Patterns that would imply the skill is asking the host to execute
# Python that imports opshub directly — that is the ②→① boundary
# violation Phase 10 plan §3-D explicitly forbids.
_FORBIDDEN_IMPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*import\s+opshub(\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+opshub(\.|$)", re.MULTILINE),
)


@pytest.mark.parametrize("name", _REQUIRED_SKILLS)
def test_skill_does_not_import_opshub_module(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    for pattern in _FORBIDDEN_IMPORT_PATTERNS:
        match = pattern.search(text)
        assert match is None, (
            f"{path} contains direct opshub import ({match.group(0)!r}); "
            f"skills must go through MCP tools or CLI (ADR-0004 §(b))"
        )


@pytest.mark.parametrize("name", _REQUIRED_SKILLS)
def test_skill_mentions_mcp_or_cli_path(name: str) -> None:
    """Every skill must reference either an MCP tool or an ``opshub`` CLI command.

    This is a loose heuristic — the skills can mention multiple of
    them — but if a skill body has neither, it almost certainly
    forgot to wire its actions to the ① core boundary.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    has_mcp_tool = bool(
        re.search(
            # Phase 10 C2 baseline + Step 1 widening (brief, graph.*,
            # source.*, embeddings.find_duplicates, propose.generate).
            # Keep the alternation single-line so the regex compiles as
            # one literal — ruff RUF003 / RUF002 don't apply in raw strings
            # here but readability still suffers above ~120 chars.
            r"\b("
            r"recall\.search|task\.list|inbox\.list|decision\.list"
            r"|task\.create|inbox\.add|connector\.sync"
            r"|brief|graph\.related|graph\.trace|graph\.expand"
            r"|source\.list|source\.get|embeddings\.find_duplicates"
            r"|propose\.generate"
            r")\b",
            text,
        )
    )
    has_cli = bool(re.search(r"\bopshub\s+\w+", text))
    assert has_mcp_tool or has_cli, (
        f"{path} must reference at least one MCP tool or ``opshub`` CLI command"
    )


# ---------------------------------------------------------------------------
# 5. Security scan (uses ``tools.skill_scan``)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _REQUIRED_SKILLS)
def test_skill_passes_security_scan(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    result = scan_skill_file(path)
    assert result.ok, f"{path} failed security scan:\n" + "\n".join(
        f"  {f.category.value} {f.rule_id} L{f.line_number}: {f.snippet}" for f in result.findings
    )
