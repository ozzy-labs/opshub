"""Format / boundary tests for secretary skill specs under ``docs/skills/``.

The skills live in ``docs/skills/<name>/SKILL.md`` as the reference
spec referenced from ``docs/secretary-agent.md`` (ADR-0004 改訂,
Phase 12 H1 — SSOT moved into opshub itself, distribution deferred).
These tests pin five invariants the Phase 10 plan §3-D / §4-D DoD
requires:

1. All five existing secretary skills exist (personal-brief /
   next-actions / reply-draft / pr-review / find-document — the
   Phase 12 H1 rename targets, see ``docs/phase-12-plan.md`` §3 H1-c).
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

# The secretary skill catalog as of Phase 12 H4.
#
# Phase 12 H1 renamed the original brief / lookup pair to
# ``personal-brief`` and ``find-document``; ``next-actions`` /
# ``reply-draft`` / ``pr-review`` keep their names.
#
# Phase 12 H2 (``docs/phase-12-plan.md`` §3 H2) adds the info-gathering
# pair ``meeting-prep`` (calendar-event-rooted, read-only) and
# ``research`` (cross-cutting topical research, read-only). Both are
# read-only and do not persist proposals — they are pure read paths
# (Step 4 of the H2 plan).
#
# Phase 12 H4 (``docs/phase-12-plan.md`` §3 H4) adds three HITL write
# skills (``inbox-triage`` / ``source-extract`` /
# ``meeting-followup``) that all route through ``propose.generate``
# (with mode dispatch) + ``propose.apply`` (ADR-0016 改訂 §決定 (l)(b)).
_REQUIRED_SKILLS: tuple[str, ...] = (
    "personal-brief",
    "next-actions",
    "reply-draft",
    "pr-review",
    "find-document",
    "meeting-prep",
    "research",
    "inbox-triage",
    "source-extract",
    "meeting-followup",
)

# Phase 12 H4 HITL write skills — the three new skills introduced in
# `docs/phase-12-plan.md` §3 H4 that share the ``propose.generate`` +
# ``propose.apply`` HITL boundary.
_PHASE_12_H4_HITL_WRITE_SKILLS: tuple[str, ...] = (
    "inbox-triage",
    "source-extract",
    "meeting-followup",
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
            # source.*, embeddings.find_duplicates, propose.generate) +
            # Phase 12 H1 (search, propose.apply). Keep the alternation
            # single-line so the regex compiles as one literal.
            r"\b("
            r"recall\.search|task\.list|inbox\.list|decision\.list"
            r"|task\.create|inbox\.add|connector\.sync"
            r"|brief|graph\.related|graph\.trace|graph\.expand"
            r"|source\.list|source\.get|embeddings\.find_duplicates"
            r"|propose\.generate|propose\.apply"
            r"|search"
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


# ---------------------------------------------------------------------------
# 6. Phase 11 semantic pins (follow-up audit Cluster A)
# ---------------------------------------------------------------------------
#
# Phase 11 added Teams + Outlook body deep retention + Office document
# extraction + the ``onedrive_drive`` local-FS connector. Several skill
# specs surfaced as drifted in the Phase 11 audit (Cluster A). These
# semantic pins follow the Phase 10 Round 2 pattern (PR #228) — assert
# the literal source_type / tool name appears so future Phase additions
# can't silently re-drift the SKILL.md.


# Phase 11 source_type literals that ``find-document`` SKILL.md
# (Phase 12 H1 rename target) must enumerate so the host
# can post-filter ``search`` / ``recall.search`` hits by user
# vocabulary ("Teams で", "Word 文書", etc.). The connector
# implementations under ``src/opshub/connectors/<name>/mapper.py``
# are the SSOT for these literals.
_FIND_DOCUMENT_PHASE_11_SOURCE_TYPES: tuple[str, ...] = (
    "teams_message",
    "word_document",
    "excel_spreadsheet",
    "powerpoint_slide_deck",
)


@pytest.mark.parametrize("source_type", _FIND_DOCUMENT_PHASE_11_SOURCE_TYPES)
def test_find_document_lists_phase_11_source_types(source_type: str) -> None:
    """``find-document`` SKILL.md must enumerate Phase 11 source_types.

    Phase 11 (Sub-issue F1-F6) added Teams chat + Office document
    extraction. find-document is the user-facing entry point for
    "find me that file" — drift here makes the new content
    effectively invisible to users even though it's indexed.
    """
    path = _SKILLS_DIR / "find-document" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert source_type in text, (
        f"{path} must mention Phase 11 source_type {source_type!r} "
        f"(SSOT: src/opshub/connectors/<name>/mapper.py)"
    )


def test_find_document_mentions_onedrive_drive_connector() -> None:
    """``find-document`` SKILL.md must mention the ``onedrive_drive`` connector.

    Phase 11 F6 (PR #248) added the ``onedrive_drive`` local-FS
    connector parallel to ``box_drive``. find-document needs to
    advertise it so users know to search the OneDrive Desktop
    sync root, not just Box Drive.
    """
    path = _SKILLS_DIR / "find-document" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "onedrive_drive" in text, (
        f"{path} must mention the ``onedrive_drive`` connector "
        f"(Phase 11 Sub-issue F6, src/opshub/connectors/onedrive_drive/)"
    )


# ---------------------------------------------------------------------------
# 7. Phase 12 H1 semantic pins (per-skill MCP dispatch pin)
# ---------------------------------------------------------------------------
#
# Phase 12 H1 (docs/phase-12-plan.md §3 H1-c) pins the MCP-direct-call
# contract: every existing Tier 1 skill must reference at least one
# MCP tool name verbatim so a host-side router can dispatch the skill
# through MCP rather than the CLI fallback. The general
# ``test_skill_mentions_mcp_or_cli_path`` already allows the disjoint
# OR (MCP OR CLI), but Phase 12 H1 narrows the existing 5 to MCP-only.

_PHASE_12_H1_MCP_DIRECT_SKILLS: tuple[str, ...] = (
    "personal-brief",
    "next-actions",
    "reply-draft",
    "pr-review",
    "find-document",
)


@pytest.mark.parametrize("name", _PHASE_12_H1_MCP_DIRECT_SKILLS)
def test_skill_mentions_at_least_one_mcp_tool_name(name: str) -> None:
    """Phase 12 H1 (ADR-0022 改訂): existing 5 skills must mention MCP tools.

    The general "MCP or CLI" pin (``test_skill_mentions_mcp_or_cli_path``)
    above accepts CLI references as well, which was the Phase 10 ladder.
    Phase 12 H1 (``docs/phase-12-plan.md`` §3 H1-c) pins the existing 5
    skills to MCP-direct-call: the SKILL.md must reference at least one
    MCP tool name verbatim so the host can route the skill through MCP
    rather than shelling out to ``opshub``.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    mcp_tool_pattern = re.compile(
        r"\b("
        r"recall\.search|task\.list|inbox\.list|decision\.list"
        r"|task\.create|inbox\.add|connector\.sync"
        r"|brief|graph\.related|graph\.trace|graph\.expand"
        r"|source\.list|source\.get|embeddings\.find_duplicates"
        r"|propose\.generate|propose\.apply"
        r"|search"
        r")\b"
    )
    assert mcp_tool_pattern.search(text), (
        f"{path} must reference at least one MCP tool name verbatim"
        " (Phase 12 H1 §3 H1-c MCP-direct-call contract)"
    )


def test_find_document_uses_search_mcp_tool() -> None:
    """``find-document`` must reference the Phase 12 H1 ``search`` MCP tool.

    The Phase 12 plan H1-c calls out ``find-document`` specifically:
    its description and body must move from CLI-based ``opshub search``
    fallback to the MCP ``search`` (FTS5) tool. A regression that drops
    the ``search`` reference is exactly what this guard catches.
    """
    path = _SKILLS_DIR / "find-document" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert re.search(r"\bsearch\b", text), (
        f"{path} must reference the MCP ``search`` (FTS5) tool (Phase 12 H1 §3 H1-c)"
    )


def test_personal_brief_description_mentions_period_vocabulary() -> None:
    """``personal-brief`` description must advertise period vocabulary.

    Phase 12 H1 (``docs/phase-12-plan.md`` §3 H1-c) expands the
    ``personal-brief`` description to include 今日 / 今週 / 今月 /
    先週 / 先月 so the host router fires the skill on these
    common time-window prompts.
    """
    path = _SKILLS_DIR / "personal-brief" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    frontmatter, _body = parse_frontmatter(text)
    description = str(frontmatter.get("description", ""))
    # At least three of the five period vocabulary tokens must appear
    # so a near-miss (e.g. only 今日 + 最近) still flags the regression.
    period_tokens = ("今日", "今週", "今月", "先週", "先月")
    hits = [token for token in period_tokens if token in description]
    assert len(hits) >= 3, (
        f"{path} description must advertise period vocabulary"
        f" (Phase 12 H1 §3 H1-c). Found {hits!r} in description={description!r}"
    )


def test_next_actions_description_mentions_period_vocabulary() -> None:
    """``next-actions`` description must advertise period vocabulary.

    Phase 12 H1 plan extends the ``next-actions`` description with
    今日 / 今週 / 来週 so the host router fires for "今週やること"
    style prompts.
    """
    path = _SKILLS_DIR / "next-actions" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    frontmatter, _body = parse_frontmatter(text)
    description = str(frontmatter.get("description", ""))
    period_tokens = ("今日", "今週", "来週")
    hits = [token for token in period_tokens if token in description]
    assert len(hits) >= 2, (
        f"{path} description must advertise period vocabulary"
        f" (Phase 12 H1 §3 H1-c). Found {hits!r} in description={description!r}"
    )


def test_reply_draft_uses_mcp_propose_generate_as_primary_path() -> None:
    """``reply-draft`` SKILL.md must reference the MCP ``propose.generate`` write tool.

    PR #231 implemented the MCP ``propose.generate`` write tool
    (``WriteCategory.PROPOSE_GENERATE``, ``src/opshub/mcp/_registry.py``).
    Before PR #231, the SKILL.md described the path as "future MCP" /
    CLI-only — that wording is now stale and must be replaced with
    the MCP-first contract. The literal ``propose.generate`` is the
    canonical tool name and must appear in the body.
    """
    path = _SKILLS_DIR / "reply-draft" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "propose.generate" in text, (
        f"{path} must reference the MCP ``propose.generate`` write tool "
        f"(PR #231, src/opshub/mcp/_registry.py WriteCategory.PROPOSE_GENERATE)"
    )


# ---------------------------------------------------------------------------
# 8. Phase 12 H4 semantic pins — HITL write skills (inbox-triage /
#    source-extract / meeting-followup)
# ---------------------------------------------------------------------------
#
# Phase 12 H4 (`docs/phase-12-plan.md` §3 H4) adds three HITL write
# skills that all route through ``propose.generate`` (with a Phase 12
# H4 ``mode`` dispatch key) and ``propose.apply``. The pins below
# capture the contract that:
#
# 1. each H4 skill mentions BOTH ``propose.generate`` AND
#    ``propose.apply`` literally so the host LLM can dispatch end-to-end
#    via MCP (no CLI fallback);
# 2. each skill names its ``mode=<dispatch>`` value verbatim so the
#    schema-level ``mode`` enum and the SKILL.md cannot drift apart;
# 3. each skill explicitly describes the HITL boundary — auto-apply
#    禁止 + 人確認必須 — at the body level so a host loader that scans
#    bodies for confirmation cues finds them.
# 4. ``propose.apply``'s MCP-level annotation is ``read_only=false``
#    (HITL boundary pin), which the skill body advertises.


_H4_MODE_PER_SKILL: dict[str, str] = {
    "inbox-triage": "inbox_triage",
    "source-extract": "source_extract",
    "meeting-followup": "meeting_followup",
}


@pytest.mark.parametrize("name", _PHASE_12_H4_HITL_WRITE_SKILLS)
def test_h4_skill_references_propose_generate_and_apply(name: str) -> None:
    """Phase 12 H4 skills must reference both ``propose.generate`` and ``propose.apply``.

    The H4 contract (`docs/phase-12-plan.md` §3 H4) is the two-stage
    HITL gate: ``propose.generate`` emits ``ProposalGenerated`` (write,
    but HITL-bounded), then ``propose.apply`` materialises the
    operator-approved candidate. A skill body that mentions only one
    half violates the contract.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "propose.generate" in text, (
        f"{path} must reference ``propose.generate`` (Phase 12 H4 HITL boundary)"
    )
    assert "propose.apply" in text, (
        f"{path} must reference ``propose.apply`` (Phase 12 H4 HITL boundary)"
    )


@pytest.mark.parametrize("name", _PHASE_12_H4_HITL_WRITE_SKILLS)
def test_h4_skill_names_mode_dispatch_key(name: str) -> None:
    """Each H4 skill must name its ``mode=<value>`` dispatch literal.

    Phase 12 H4 adds a ``mode`` enum to ``propose.generate``
    (ADR-0016 改訂 §決定 (l)(b)). The SKILL.md must name its dispatch
    value so the schema enum and the skill body cannot drift.
    """
    expected_mode = _H4_MODE_PER_SKILL[name]
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert expected_mode in text, (
        f"{path} must name its ``mode={expected_mode}`` dispatch literal"
        " (Phase 12 H4, ADR-0016 §決定 (l)(b))"
    )


@pytest.mark.parametrize("name", _PHASE_12_H4_HITL_WRITE_SKILLS)
def test_h4_skill_advertises_hitl_boundary(name: str) -> None:
    """Each H4 skill body must explicitly forbid auto-apply.

    ADR-0016 §決定 (c) pins the HITL contract: ``propose.apply`` must
    require operator confirmation. The Phase 12 plan §3 H4 DoD calls
    out a "HITL boundary test pin" — the body must explicitly say
    "auto-apply 禁止" (or equivalent) so a host loader / reviewer can
    not silently skip the confirmation.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # Accept several Japanese phrasings — the body is required to say
    # auto-apply is forbidden / user confirmation is required, in any
    # of the canonical wordings used across the existing 5 skills.
    hitl_phrases = (
        "auto-apply",  # mentioned in the prohibition context
        "人確認",
        "HITL",
    )
    hits = [phrase for phrase in hitl_phrases if phrase in text]
    assert len(hits) >= 2, (
        f"{path} must advertise the HITL boundary explicitly (Phase 12 H4 DoD)."
        f" Found phrases: {hits!r}"
    )


def test_h4_propose_apply_annotation_is_not_read_only() -> None:
    """Phase 12 H4 HITL boundary test pin — ``propose.apply`` annotation.

    The plan §3 H4 DoD: "propose.apply の annotation = read_only=false
    確認". The MCP tool annotation must surface this so HITL-aware
    hosts (e.g. Claude Code, Codex CLI) prompt before invocation.
    This pin imports the registry directly and asserts the policy
    flags on the ``propose.apply`` tool spec.
    """
    from opshub.mcp._registry import build_tool_specs

    # Pass minimal stub handlers — build_tool_specs takes a mapping of
    # tool name to handler callable and stamps them onto the specs. We
    # only inspect ``policy``, not the handler itself.
    async def _noop(_arguments: object) -> str:  # pragma: no cover - never called
        return "{}"

    # ``build_tool_specs`` requires handlers for every tool. Build a
    # mapping that returns ``_noop`` for every known tool name so the
    # spec list materialises without engine wiring.
    handler_names = (
        "task.list",
        "inbox.list",
        "decision.list",
        "recall.search",
        "task.create",
        "inbox.add",
        "connector.sync",
        "brief",
        "graph.related",
        "graph.trace",
        "graph.expand",
        "source.list",
        "source.get",
        "embeddings.find_duplicates",
        "propose.generate",
        "propose.apply",
        "search",
    )
    handlers: dict[str, object] = dict.fromkeys(handler_names, _noop)

    specs = build_tool_specs(handlers=handlers)  # type: ignore[arg-type]
    apply_spec = next((s for s in specs if s.name == "propose.apply"), None)
    assert apply_spec is not None, "propose.apply must be registered (Phase 12 H1)"

    # The DoD literal: ``read_only=false``. The HITL boundary lives
    # here — hosts that honour ``readOnlyHint=false`` will prompt
    # before invoking, satisfying the §決定 (c) contract.
    assert apply_spec.policy.read_only is False, (
        "propose.apply policy must have read_only=False (Phase 12 H4 HITL boundary"
        " test pin, docs/phase-12-plan.md §3 H4 DoD)"
    )
    # Sanity: also pin destructive=False + idempotent=True so the
    # boundary contract pinned by ADR-0022 改訂 stays observable.
    assert apply_spec.policy.destructive is False, (
        "propose.apply policy must have destructive=False (ADR-0022 改訂)"
    )
    assert apply_spec.policy.idempotent is True, (
        "propose.apply policy must have idempotent=True (ADR-0022 改訂)"
    )


def test_h4_propose_generate_schema_includes_mode_enum() -> None:
    """Phase 12 H4 (ADR-0016 §決定 (l)(b)) — ``mode`` enum dispatch key.

    The Phase 12 plan §3 H4 calls out that ``propose.generate`` must
    expose a ``mode`` argument whose values dispatch to ``inbox_triage``
    / ``source_extract`` / ``meeting_followup`` (plus the implicit
    reply-draft mode signalled via ``reply_to_source_id``). This pin
    verifies the schema enum so the SKILL.md side and the registry
    side cannot drift.
    """
    from opshub.mcp._registry import build_tool_specs

    async def _noop(_arguments: object) -> str:  # pragma: no cover - never called
        return "{}"

    handler_names = (
        "task.list",
        "inbox.list",
        "decision.list",
        "recall.search",
        "task.create",
        "inbox.add",
        "connector.sync",
        "brief",
        "graph.related",
        "graph.trace",
        "graph.expand",
        "source.list",
        "source.get",
        "embeddings.find_duplicates",
        "propose.generate",
        "propose.apply",
        "search",
    )
    handlers: dict[str, object] = dict.fromkeys(handler_names, _noop)

    specs = build_tool_specs(handlers=handlers)  # type: ignore[arg-type]
    generate_spec = next((s for s in specs if s.name == "propose.generate"), None)
    assert generate_spec is not None
    schema = dict(generate_spec.input_schema)
    properties = dict(schema["properties"])
    assert "mode" in properties, (
        "propose.generate input schema must declare a ``mode`` property"
        " (Phase 12 H4, ADR-0016 §決定 (l)(b))"
    )
    mode_schema = dict(properties["mode"])
    enum = tuple(mode_schema.get("enum", ()))
    assert set(enum) == {"inbox_triage", "source_extract", "meeting_followup"}, (
        f"propose.generate ``mode`` enum must equal the H4 dispatch triple;"
        f" got {enum!r}. handoff_draft / announcement_draft are excluded"
        f" (text-only, ADR-0016 §決定 (l)(b) Negative arm)."
    )


# ---------------------------------------------------------------------------
# 9. Phase 12 H2 semantic pins (info gathering skills)
# ---------------------------------------------------------------------------
#
# Phase 12 H2 (``docs/phase-12-plan.md`` §3 H2) introduces two new
# read-only info-gathering skills: ``meeting-prep`` (calendar-event
# rooted, builds context for an upcoming meeting) and ``research``
# (cross-cutting topical research over the opshub memory layer).
#
# These pins lock the MCP-direct-call contract for each new skill so
# host routers can dispatch them through MCP without falling back to
# the CLI shell. The contract is:
#
# * ``meeting-prep`` walks calendar events via ``source.list`` with
#   ``source_type=ms365_calendar`` and the H1 ``observed_after`` /
#   ``observed_before`` time filter, then enriches with
#   ``recall.search`` and ``graph.related``.
# * ``research`` combines ``recall.search`` (semantic) + ``search``
#   (FTS5, H1) + ``graph.related`` / ``graph.expand`` (entity
#   neighbourhood) + ``brief`` (LLM-backed summary).
# * Both skills are read-only — they MUST NOT reference
#   ``propose.generate`` / ``propose.apply`` (HITL write boundary).


_MEETING_PREP_REQUIRED_TOOLS: tuple[str, ...] = (
    "source.list",
    "recall.search",
    "graph.related",
)


@pytest.mark.parametrize("tool_name", _MEETING_PREP_REQUIRED_TOOLS)
def test_meeting_prep_dispatches_required_mcp_tools(tool_name: str) -> None:
    """``meeting-prep`` SKILL.md must dispatch through the H2 tool chain.

    The Phase 12 plan §3 H2 pins the call sequence as
    ``source.list (source_type=ms365_calendar, observed_after/before)``
    → ``recall.search`` (participants / topic) →
    ``graph.related`` (related decisions / docs). The host router
    needs every literal tool name in the body so it can dispatch
    without guessing.
    """
    path = _SKILLS_DIR / "meeting-prep" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert tool_name in text, (
        f"{path} must reference MCP tool {tool_name!r} verbatim (Phase 12 H2 §3 H2 dispatch chain)"
    )


def test_meeting_prep_uses_calendar_source_type() -> None:
    """``meeting-prep`` must filter ``source.list`` by ``ms365_calendar``.

    The Phase 11 ms365 connector maps Calendar events to
    ``source_type = "ms365_calendar"`` (SSOT:
    ``src/opshub/connectors/ms365/mapper.py``
    ``CALENDAR_SOURCE_TYPE``). The skill body MUST reference that
    literal so the host filters the right rows.
    """
    path = _SKILLS_DIR / "meeting-prep" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "ms365_calendar" in text, (
        f"{path} must reference the ``ms365_calendar`` source_type "
        f"(SSOT: src/opshub/connectors/ms365/mapper.py)"
    )


def test_meeting_prep_uses_h1_observed_time_filter() -> None:
    """``meeting-prep`` must use the H1 ``observed_after`` / ``observed_before`` filter.

    Phase 12 H1 (ADR-0022 改訂) added physical-column time filters on
    ``source.list`` — ``observed_after`` / ``observed_before`` against
    ``sources.observed_at``. ``meeting-prep`` is the canonical user
    of that filter (calendar events for an upcoming window).
    """
    path = _SKILLS_DIR / "meeting-prep" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "observed_after" in text and "observed_before" in text, (
        f"{path} must reference the H1 ``observed_after`` / "
        f"``observed_before`` time filter on ``source.list``"
    )


def test_meeting_prep_is_read_only() -> None:
    """``meeting-prep`` must not invoke any write MCP tools.

    Phase 12 plan §3 H2 explicitly classifies ``meeting-prep`` as
    read-only (no persist). The HITL write counterpart is
    ``meeting-followup`` (Phase 12 H4). If a future edit slips a
    ``propose.generate`` / ``propose.apply`` / ``task.create`` /
    ``inbox.add`` / ``connector.sync`` into the body, the boundary
    has drifted.
    """
    path = _SKILLS_DIR / "meeting-prep" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # Allow disallowed tool names to appear only inside a fenced
    # "do not call" passage — but pin the simpler invariant: the
    # body's call-order section uses ``tool: <name>`` for prescribed
    # tools, so disallowed tools must not appear with that exact
    # prefix anywhere in the file.
    for forbidden in (
        "tool: propose.generate",
        "tool: propose.apply",
        "tool: task.create",
        "tool: inbox.add",
        "tool: connector.sync",
    ):
        assert forbidden not in text, (
            f"{path} must not invoke {forbidden!r} — meeting-prep is "
            f"read-only (Phase 12 plan §3 H2). HITL write belongs to "
            f"meeting-followup (Phase 12 H4)."
        )


_RESEARCH_REQUIRED_TOOLS: tuple[str, ...] = (
    "recall.search",
    "search",
    "graph.related",
    "graph.expand",
    "brief",
)


@pytest.mark.parametrize("tool_name", _RESEARCH_REQUIRED_TOOLS)
def test_research_dispatches_required_mcp_tools(tool_name: str) -> None:
    """``research`` SKILL.md must dispatch through the H2 tool chain.

    The Phase 12 plan §3 H2 pins the call sequence as
    ``recall.search`` (semantic) + ``search`` (FTS5, H1) +
    ``graph.related`` / ``graph.expand`` (entity neighbourhood) +
    ``brief`` (LLM-backed summary). Each literal must appear so the
    host can dispatch each step without guessing.
    """
    path = _SKILLS_DIR / "research" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    # ``search`` is a substring of ``recall.search``, so use a
    # word-boundary regex to make the search-tool pin meaningful.
    pattern = re.compile(rf"(?<!\.)\b{re.escape(tool_name)}\b")
    assert pattern.search(text), (
        f"{path} must reference MCP tool {tool_name!r} verbatim (Phase 12 H2 §3 H2 dispatch chain)"
    )


def test_research_is_read_only() -> None:
    """``research`` must not invoke any write MCP tools.

    Phase 12 plan §3 H2 classifies ``research`` as read-only — the
    cross-cutting investigation does not persist proposals. Write
    paths belong to other skills (reply-draft / inbox-triage /
    source-extract / meeting-followup).
    """
    path = _SKILLS_DIR / "research" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    for forbidden in (
        "tool: propose.generate",
        "tool: propose.apply",
        "tool: task.create",
        "tool: inbox.add",
        "tool: connector.sync",
    ):
        assert forbidden not in text, (
            f"{path} must not invoke {forbidden!r} — research is read-only (Phase 12 plan §3 H2)."
        )


# ---------------------------------------------------------------------------
# 9. Phase 12 H5 — draft skills (handoff-draft + announcement-draft)
# ---------------------------------------------------------------------------
#
# Phase 12 H5 (docs/phase-12-plan.md §3 H5) adds two text-only draft skills.
# ADR-0016 §決定 (l)(a) pins the persist boundary by "返信元 source の有
# 無": reply-draft persists via propose.generate + propose.apply, but
# handoff-draft / announcement-draft return text only — no candidate
# persist / apply path. The tests below pin (a) presence, (b) format /
# scan / budget reuse via dedicated parametrize, (c) per-skill MCP
# dispatch (handoff: task.list + decision.list + recall.search +
# graph.related; announcement: recall.search + decision.list + brief),
# and (d) text-only boundary: no propose.generate / propose.apply
# references in body, no write tool references at all.
#
# We deliberately do not extend ``_REQUIRED_SKILLS`` here because the
# Phase 12 plan §3 Wave 配置 calls out merge-conflict mitigation: each
# Wave 2 sub-issue (H2/H3/H4/H5) edits this file in a dedicated section
# so the existing 5-skill parametrize block stays as-is and the new
# parametrize blocks land side by side.

_PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS: tuple[str, ...] = (
    "handoff-draft",
    "announcement-draft",
)

# Per-skill required MCP tool surface (Phase 12 plan §3 H5).
# - handoff-draft: task.list (state=in_progress) + decision.list +
#   recall.search + graph.related
# - announcement-draft: recall.search + decision.list (recorded_after=
#   last_release) + brief (announcement tone)
_PHASE_12_H5_REQUIRED_MCP_TOOLS: dict[str, tuple[str, ...]] = {
    "handoff-draft": ("task.list", "decision.list", "recall.search", "graph.related"),
    "announcement-draft": ("recall.search", "decision.list", "brief"),
}

# Tools that imply candidate persist / apply path (ADR-0016 §決定 (l)(a)
# / (b)). handoff-draft / announcement-draft must NOT reference these —
# their SKILL.md must explicitly stay on the read-only side.
#
# ``propose.generate`` covers the dispatch-key path (§決定 (l)(b)),
# ``propose.apply`` covers the persist commit, and ``task.create`` /
# ``inbox.add`` / ``connector.sync`` cover the other write tools the
# scope-out section forbids in body text.
_PHASE_12_H5_FORBIDDEN_WRITE_TOOLS: tuple[str, ...] = (
    "propose.generate",
    "propose.apply",
    "task.create",
    "inbox.add",
    "connector.sync",
)


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_spec_exists(name: str) -> None:
    """Phase 12 H5 SKILL.md files exist at the canonical path."""
    path = _SKILLS_DIR / name / "SKILL.md"
    assert path.is_file(), f"missing Phase 12 H5 draft skill spec: {path}"


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_frontmatter(name: str) -> None:
    """Phase 12 H5 SKILL.md frontmatter has matching ``name`` + non-empty ``description``."""
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


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_body_within_budget(name: str) -> None:
    """Phase 12 H5 SKILL.md body stays inside the 5000-word ceiling."""
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    words = body.split()
    assert len(words) <= _BODY_WORD_CEILING, (
        f"{path} body has {len(words)} words; "
        f"≤{_BODY_WORD_CEILING} expected (Phase 10 plan §3-D 5k tokens budget)"
    )


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_passes_security_scan(name: str) -> None:
    """Phase 12 H5 SKILL.md is clean under ``tools.skill_scan``.

    The Phase 12 H5 DoD lists ``skill_scan pass`` as a required gate.
    This pin makes a future contributor unable to land a SKILL.md that
    fails the security scan without also editing the scanner.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    result = scan_skill_file(path)
    assert result.ok, f"{path} failed security scan:\n" + "\n".join(
        f"  {f.category.value} {f.rule_id} L{f.line_number}: {f.snippet}" for f in result.findings
    )


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_does_not_import_opshub_module(name: str) -> None:
    """Phase 12 H5 SKILL.md must not contain direct ``import opshub`` lines.

    Same ②→① boundary as the existing 5 skills: skills go through MCP
    tools, never through Python module imports of ``opshub.*``.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    for pattern in _FORBIDDEN_IMPORT_PATTERNS:
        match = pattern.search(text)
        assert match is None, (
            f"{path} contains direct opshub import ({match.group(0)!r}); "
            f"skills must go through MCP tools (ADR-0004 §(b))"
        )


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_dispatches_required_mcp_tools(name: str) -> None:
    """Phase 12 H5 per-skill MCP dispatch pin.

    The Phase 12 plan §3 H5 maps each draft skill to a specific MCP
    tool set:

    - ``handoff-draft``: ``task.list`` (state=in_progress) +
      ``decision.list`` + ``recall.search`` + ``graph.related``
    - ``announcement-draft``: ``recall.search`` + ``decision.list``
      (recorded_after=last_release) + ``brief`` (announcement tone)

    Every required tool name must appear verbatim in the SKILL.md body
    so a host-side router can dispatch reliably.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    for tool in _PHASE_12_H5_REQUIRED_MCP_TOOLS[name]:
        assert tool in text, (
            f"{path} must reference MCP tool {tool!r} verbatim "
            f"(Phase 12 plan §3 H5 per-skill MCP dispatch pin)"
        )


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_is_text_only_no_persist_path(name: str) -> None:
    """Phase 12 H5 text-only boundary pin (ADR-0016 §決定 (l)(a) / (b)).

    handoff-draft / announcement-draft are text-only: they must NOT
    reference any candidate persist / apply MCP tool in their body.
    The forbidden set covers (a) ``propose.generate`` (dispatch-key
    persist path frozen to 4 modes that excludes handoff / announcement
    per ADR-0016 §決定 (l)(b)), (b) ``propose.apply`` (candidate commit),
    and (c) ``task.create`` / ``inbox.add`` / ``connector.sync`` (other
    write tools the scope-out section of each SKILL.md forbids).

    ``reply-draft`` is the *only* draft skill that may reference these.

    This pin makes the OQ2=B / ADR-0016 §決定 (l)(a) decision
    machine-checked so a future contributor cannot silently re-add a
    persist path by editing the SKILL.md body.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    # A SKILL.md is allowed to *name* the forbidden tools in a
    # "できないこと / やらない" or "参考" block to explicitly call out
    # that the persist path does NOT apply. To distinguish "uses the
    # tool" from "documents the absence", we require the forbidden
    # tool name to appear only inside the explicit scope-out section
    # (the "できないこと / やらない" heading is the canonical marker)
    # and not as an actionable ``tool: <name>`` MCP invocation block.
    invocation_pattern = re.compile(
        r"^\s*tool:\s*(?P<tool>[a-z_.]+)\s*$",
        re.MULTILINE,
    )
    invoked_tools = {match.group("tool") for match in invocation_pattern.finditer(text)}

    forbidden_invocations = invoked_tools.intersection(_PHASE_12_H5_FORBIDDEN_WRITE_TOOLS)
    assert not forbidden_invocations, (
        f"{path} contains MCP tool invocation block(s) for forbidden "
        f"write tool(s) {sorted(forbidden_invocations)!r}; Phase 12 H5 "
        f"draft skills are text-only (ADR-0016 §決定 (l)(a) / (b))"
    )


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_documents_propose_generate_absence(name: str) -> None:
    """Phase 12 H5 text-only static lint — ``propose.generate`` non-call (M11).

    Cluster B M11 audit pin: the existing top-level text-only test
    (``test_phase_12_h5_draft_skill_is_text_only_no_persist_path``)
    catches the case where the SKILL.md actively invokes ``propose.generate``
    (via the ``tool: propose.generate`` block). But there is a subtler
    drift: a future contributor could simply *mention* ``propose.generate``
    in prose ("we could also use propose.generate here") without an
    explicit "we don't call it" disclaimer, and the read-only boundary
    would silently erode even though the actionable ``tool:`` block is
    absent. ADR-0016 §決定 (l)(a) requires handoff-draft / announcement-
    draft to stay text-only — this pin enforces that ``propose.generate``
    is either:

    * **absent entirely** (the simplest case — skill does not mention it), or
    * **mentioned only inside an explicit non-call statement**
      (``呼ばない`` / ``経由せず`` / ``使わない`` / ``persist しない`` /
      ``does not invoke`` / similar disclaimer phrasing).

    Any other appearance of ``propose.generate`` in the body (including
    a stray code snippet or "future work" note) is a regression that
    silently invites a host to wire the persist path back in.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    if "propose.generate" not in text:
        return  # No mention at all — the strongest text-only guarantee.

    # Disclaimer markers that explicitly call out the absence of the
    # persist path. Mirrors the markers used in the H3 read-only test
    # above plus the canonical phrasings the existing handoff-draft /
    # announcement-draft SKILL.md files use (verified at audit time:
    # handoff-draft uses "propose.generate を経由せず" / "呼ばない").
    non_call_markers = (
        "呼ばない",
        "呼び出さない",
        "経由せず",
        "使わない",
        "persist しない",
        "持たない",
        "存在しない",  # e.g. "announcement_draft mode は存在しない"
        "ない (",  # e.g. "candidate 保存経路はない (propose.generate を呼ばない)"
        "scope 外",
        "本 skill では",
        "本 skill scope 外",
        "別 skill",
        "does not invoke",
        "does not call",
    )

    # Inspect every line that mentions ``propose.generate``; each MUST
    # also carry at least one non-call disclaimer marker. We allow the
    # disclaimer to live on the same line *or* the previous line (so
    # markdown bullet structures like "- propose.generate を呼ばない"
    # work as well as paragraph-style "...である。propose.generate を
    # 呼ばないため..." constructions).
    lines = text.splitlines()
    offending: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if "propose.generate" not in line:
            continue
        window = "\n".join(lines[max(0, index - 1) : index + 2])
        if any(marker in window for marker in non_call_markers):
            continue
        offending.append((index + 1, line.strip()))

    assert not offending, (
        f"{path} mentions ``propose.generate`` outside an explicit non-call"
        f" disclaimer (Phase 12 H5 M11 audit pin, ADR-0016 §決定 (l)(a)):\n"
        + "\n".join(f"  L{lineno}: {snippet!r}" for lineno, snippet in offending)
    )


@pytest.mark.parametrize("name", _PHASE_12_H5_TEXT_ONLY_DRAFT_SKILLS)
def test_phase_12_h5_draft_skill_reference_directory_is_text_only(name: str) -> None:
    """Phase 12 H5 text-only boundary — recursive sweep over reference/ (L8a).

    Cluster B L8a audit pin: the existing
    ``test_phase_12_h5_draft_skill_is_text_only_no_persist_path`` scans
    only the top-level ``SKILL.md``. The Anthropic Skills progressive-
    disclosure pattern (L3) puts deeper docs under
    ``docs/skills/<name>/reference/`` — a future contributor could land
    a ``tool: propose.apply`` invocation inside ``reference/persist.md``
    and the existing top-level scan would silently miss it.

    This pin widens the scan: every ``*.md`` file under the skill's
    directory (top-level SKILL.md + all of ``reference/``) MUST NOT
    contain any of the forbidden ``tool: <write>`` invocation blocks.
    """
    skill_dir = _SKILLS_DIR / name
    assert skill_dir.is_dir(), f"missing skill directory: {skill_dir}"

    invocation_pattern = re.compile(
        r"^\s*tool:\s*(?P<tool>[a-z_.]+)\s*$",
        re.MULTILINE,
    )

    offending: list[tuple[Path, str]] = []
    for md_path in sorted(skill_dir.rglob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        invoked_tools = {match.group("tool") for match in invocation_pattern.finditer(text)}
        forbidden = invoked_tools.intersection(_PHASE_12_H5_FORBIDDEN_WRITE_TOOLS)
        for tool in sorted(forbidden):
            offending.append((md_path, tool))

    assert not offending, (
        f"Phase 12 H5 skill {name!r}: forbidden write-tool invocation(s)"
        f" found under {skill_dir} (L8a audit pin):\n"
        + "\n".join(f"  {p}: tool: {t}" for p, t in offending)
    )


def test_phase_12_h5_handoff_draft_references_adr_0016_decision_l() -> None:
    """``handoff-draft`` SKILL.md must cite ADR-0016 §決定 (l) (text-only rationale).

    ADR-0016 §決定 (l) is the authoritative ruling that handoff-draft
    is text-only. The SKILL.md must reference it so a reader can
    trace the design decision without having to re-derive the persist
    boundary from first principles.
    """
    path = _SKILLS_DIR / "handoff-draft" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "ADR-0016" in text and "(l)" in text, (
        f"{path} must reference ADR-0016 §決定 (l) (Draft 系統一方針 / text-only rationale)"
    )


def test_phase_12_h5_announcement_draft_references_adr_0016_decision_l() -> None:
    """``announcement-draft`` SKILL.md must cite ADR-0016 §決定 (l) (text-only rationale).

    Same rationale as ``handoff-draft``: ADR-0016 §決定 (l) is the
    SSOT for the text-only persist boundary, the SKILL.md must
    reference it explicitly.
    """
    path = _SKILLS_DIR / "announcement-draft" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "ADR-0016" in text and "(l)" in text, (
        f"{path} must reference ADR-0016 §決定 (l) (Draft 系統一方針 / text-only rationale)"
    )


# ---------------------------------------------------------------------------
# 9. Phase 12 H3 semantic pins (analysis skills)
# ---------------------------------------------------------------------------
#
# Phase 12 H3 (docs/phase-12-plan.md §3 H3) adds two analysis skills:
# ``external-brief`` (paired with ``personal-brief``) and
# ``decision-rationale``. These pins enforce:
# - presence + frontmatter + body budget + boundary + security scan
#   (re-using the generic invariants by extending _REQUIRED_SKILLS via
#   a parametrised set below)
# - per-skill MCP dispatch pin: each analysis skill mentions the MCP
#   tool names it depends on verbatim so the host router can dispatch
# - tone-pair pin: ``external-brief`` mentions ``personal-brief`` pair
# - HITL boundary pin: neither skill mentions write tools (analysis
#   skills are read-only by design; the only durable-state write the
#   plan allows from these skills is none)

_PHASE_12_H3_ANALYSIS_SKILLS: tuple[str, ...] = (
    "external-brief",
    "decision-rationale",
)


@pytest.mark.parametrize("name", _PHASE_12_H3_ANALYSIS_SKILLS)
def test_phase12_h3_skill_spec_exists(name: str) -> None:
    """Phase 12 H3 analysis skills must exist on disk.

    Mirrors ``test_skill_spec_exists`` but pinned to the H3 set so a
    regression that drops one of the new SKILL.md files is caught
    even if ``_REQUIRED_SKILLS`` has not yet been widened to include
    the H3 additions (the H6 closeout PR will eventually unify them).
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    assert path.is_file(), f"missing Phase 12 H3 analysis skill spec: {path}"


@pytest.mark.parametrize("name", _PHASE_12_H3_ANALYSIS_SKILLS)
def test_phase12_h3_skill_frontmatter_has_name_and_description(name: str) -> None:
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


@pytest.mark.parametrize("name", _PHASE_12_H3_ANALYSIS_SKILLS)
def test_phase12_h3_skill_body_within_budget(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    words = body.split()
    assert len(words) <= _BODY_WORD_CEILING, (
        f"{path} body has {len(words)} words; "
        f"≤{_BODY_WORD_CEILING} expected (Phase 10 plan §3-D 5k tokens budget)"
    )


@pytest.mark.parametrize("name", _PHASE_12_H3_ANALYSIS_SKILLS)
def test_phase12_h3_skill_does_not_import_opshub_module(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    for pattern in _FORBIDDEN_IMPORT_PATTERNS:
        match = pattern.search(text)
        assert match is None, (
            f"{path} contains direct opshub import ({match.group(0)!r}); "
            f"skills must go through MCP tools or CLI (ADR-0004 §(b))"
        )


@pytest.mark.parametrize("name", _PHASE_12_H3_ANALYSIS_SKILLS)
def test_phase12_h3_skill_passes_security_scan(name: str) -> None:
    path = _SKILLS_DIR / name / "SKILL.md"
    result = scan_skill_file(path)
    assert result.ok, f"{path} failed security scan:\n" + "\n".join(
        f"  {f.category.value} {f.rule_id} L{f.line_number}: {f.snippet}" for f in result.findings
    )


# Per-skill MCP dispatch pin for H3 analysis skills. Each skill MUST
# reference its declared MCP tools by literal name so the host router
# can verify the SKILL.md is in sync with opshub's MCP surface
# (Phase 12 plan §3 H3 DoD).

_PHASE_12_H3_REQUIRED_MCP_TOOLS: dict[str, tuple[str, ...]] = {
    # external-brief: task.list (state=completed, updated_after) +
    # decision.list (recorded_after) + brief (external tone)
    "external-brief": ("task.list", "decision.list", "brief"),
    # decision-rationale: decision.list (filter by topic) +
    # graph.trace (decision → source / proposal / prior decision) +
    # recall.search (related context)
    "decision-rationale": ("decision.list", "graph.trace", "recall.search"),
}


@pytest.mark.parametrize("name", _PHASE_12_H3_ANALYSIS_SKILLS)
def test_phase12_h3_skill_mentions_required_mcp_tools(name: str) -> None:
    """Phase 12 H3 analysis skills must mention required MCP tools by name.

    The per-skill MCP dispatch pin enforces that each new skill's
    body references the literal MCP tool names declared in the H3
    plan (``docs/phase-12-plan.md`` §3 H3). A regression that swaps
    in a different tool (or drops one) is caught immediately.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    required = _PHASE_12_H3_REQUIRED_MCP_TOOLS[name]
    missing = [tool for tool in required if tool not in text]
    assert not missing, (
        f"{path} must reference Phase 12 H3 MCP tools {required!r}; "
        f"missing {missing!r} (docs/phase-12-plan.md §3 H3)"
    )


def test_phase12_h3_external_brief_uses_time_filter_arguments() -> None:
    """``external-brief`` must reference the physical-column time filter args.

    The Phase 12 H3 plan pins task.list ``updated_after`` and
    decision.list ``recorded_after`` as the surface for the
    ``state=completed`` / "this week" combination (the projection
    has no ``completed_at`` column, so this is the documented
    approximation — §3 H3 note). A regression that omits the time
    filter literals would silently dispatch unfiltered ``task.list``
    calls and blow the context window.
    """
    path = _SKILLS_DIR / "external-brief" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    for arg in ("updated_after", "recorded_after"):
        assert arg in text, (
            f"{path} must reference time-filter argument {arg!r} "
            f"(Phase 12 H1 ADR-0022 改訂、Phase 12 H3 §3 H3 note on "
            f"completed_at approximation)"
        )


def test_phase12_h3_external_brief_advertises_personal_brief_pair() -> None:
    """``external-brief`` description must advertise pairing with ``personal-brief``.

    Phase 12 H3 plan (§3 H3) explicitly pins external-brief as the
    "外向き" pair of "自分向け" ``personal-brief``. The host router
    relies on the description hint to disambiguate when the user
    says "まとめて" (which could fire either skill). A regression
    that drops the pair reference would collapse the two skills'
    routing into a single ambiguous trigger.
    """
    path = _SKILLS_DIR / "external-brief" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    frontmatter, _body = parse_frontmatter(text)
    description = str(frontmatter.get("description", ""))
    assert "personal-brief" in description, (
        f"{path} description must mention pairing with ``personal-brief`` "
        f"(Phase 12 H3 plan §3 H3 pair structure). Description: {description!r}"
    )


def test_phase12_h3_decision_rationale_uses_graph_trace_depth_hint() -> None:
    """``decision-rationale`` must reference graph.trace depth semantics.

    Phase 12 H3 plan binds decision-rationale to ``graph.trace`` for
    backward provenance walks (decision → source / proposal / prior
    decision). ADR-0017 §(e) caps depth at 10 with default 3. The
    skill body should reference depth so the host doesn't blindly
    use default-3 in cases where the chain is deeper.
    """
    path = _SKILLS_DIR / "decision-rationale" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "depth" in text, (
        f"{path} must reference graph.trace ``depth`` semantics (ADR-0017 §(e), default 3, max 10)"
    )


@pytest.mark.parametrize("name", _PHASE_12_H3_ANALYSIS_SKILLS)
def test_phase12_h3_skill_is_read_only(name: str) -> None:
    """Phase 12 H3 analysis skills must not reference write MCP tools.

    Both ``external-brief`` and ``decision-rationale`` are read-only
    analysis skills (no persist, no proposal, no task creation).
    The Phase 12 H3 plan explicitly puts them under the "read 自律
    OK (10 件)" bucket alongside personal-brief / next-actions etc.
    A regression that introduces a write-tool reference (task.create
    / inbox.add / connector.sync / propose.apply / propose.generate)
    in the body or description would cross the HITL boundary and is
    explicitly out of scope per ADR-0016 改訂 §決定 (l).

    We allow the tokens to appear inside a "やらない" / "scope 外"
    disclaimer prefixed by a leading negation (e.g. "は本 skill では
    呼ばない"). The check is intentionally heuristic: presence of
    the literal write tool name OUTSIDE such a disclaimer is the
    regression we care about.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    write_tools = (
        "task.create",
        "inbox.add",
        "connector.sync",
        "propose.apply",
        "propose.generate",
    )
    for tool in write_tools:
        for raw_line in text.splitlines():
            if tool not in raw_line:
                continue
            # Allow disclaimer lines that explicitly mark the tool
            # as out-of-scope. The keywords cover the standard
            # phrasing used across opshub skills.
            line = raw_line.strip()
            if any(
                marker in line
                for marker in (
                    "呼ばない",
                    "は本 skill",
                    "本 skill では",
                    "本 skill scope 外",
                    "scope 外",
                    "別 skill",
                    "別経路",
                    "別操作",
                    "write 経路",
                    "write tool",
                )
            ):
                continue
            raise AssertionError(
                f"{path} references write MCP tool {tool!r} outside a"
                " disclaimer line. Phase 12 H3 analysis skills are"
                " strictly read-only (ADR-0016 改訂 §決定 (l))."
                f" Offending line: {line!r}"
            )
