"""Phase 18-C semantic pins for the ``personal-brief`` SKILL.md SSOT.

ADR-0033 §決定 (c) wires the new ``slack.demand.list`` MCP tool into
``personal-brief`` so the period summary ("今日のまとめ" / "今週どう
なってる" / "先月の振り返り") includes Slack ``<@self>`` mention and
DM activity alongside the standard tasks / inbox / decisions inputs.
These pins capture the contract that:

* the SKILL.md body references ``slack.demand.list`` verbatim so the
  host router can dispatch through MCP without falling back to a
  CLI shell;
* the body uses the ``since_ts`` filter (the Phase 18-C canonical
  way to scope demand signals to the period being summarised);
* the description / body advertise the demand signal so a user
  asking "今週どうなってる" gets the Slack ping context, not just
  ``tasks`` / ``inbox`` / ``decisions``;
* the dispatch list still includes the Phase 12 H1 baseline
  (``brief`` + ``recall.search`` + ``task.list`` + ``inbox.list`` +
  ``decision.list``) so the period summary keeps every input
  source the pre-Phase-18 version had.

Snapshot-style: the tests pin the **literals** in the body rather
than driving the MCP handler — the handler is covered by
:mod:`tests.unit.mcp.test_slack_demand`.
"""

from __future__ import annotations

from pathlib import Path

from tools.skill_scan import parse_frontmatter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_PATH = _REPO_ROOT / "docs" / "skills" / "personal-brief" / "SKILL.md"


def _read_skill() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def test_personal_brief_references_slack_demand_list_mcp_tool() -> None:
    """``personal-brief`` SKILL.md must reference ``slack.demand.list`` literally.

    Phase 18-C ([ADR-0033](docs/adr/0033-slack-mention-demand-digest.md)
    §決定 (c)) added the new read tool. The host router needs the
    literal tool name in the body so it can dispatch the demand
    signal step without guessing.
    """
    text = _read_skill()
    assert "slack.demand.list" in text, (
        f"{_SKILL_PATH} must reference the Phase 18-C ``slack.demand.list``"
        " MCP tool verbatim (ADR-0033 §決定 (c))"
    )


def test_personal_brief_demand_filter_targets_dm_and_mention() -> None:
    """The body must filter on ``dm`` + ``mention`` demand kinds.

    Phase 18-B writes ``dm`` and ``mention`` rows (group-DM
    ``<@self>`` mentions land in the mention row). The Phase 18-C
    canonical filter is the same two kinds; a body that omits
    the filter would also surface a future ``mpim`` row when
    Phase 19+ starts writing them, which is fine for forward
    compatibility but loses the explicit signal in the SKILL.md.
    """
    text = _read_skill()
    has_demand_kinds_marker = "demand_kinds" in text
    has_kind_values = "dm" in text and "mention" in text
    assert has_demand_kinds_marker and has_kind_values, (
        f'{_SKILL_PATH} must use the ``demand_kinds=["dm", "mention"]``'
        " filter when calling ``slack.demand.list`` (Phase 18-C canonical"
        " dispatch key)"
    )


def test_personal_brief_uses_since_ts_to_scope_to_period() -> None:
    """The body must use ``since_ts`` to bound demand signals by the period.

    The whole point of ``personal-brief`` is "what happened in the
    period the user asked about". The Phase 18-C call site must
    therefore translate the period lower bound into a ``since_ts``
    epoch float — without it, the skill would surface demand rows
    older than the period window.
    """
    text = _read_skill()
    assert "since_ts" in text, (
        f"{_SKILL_PATH} must reference the ``since_ts`` argument when"
        " calling ``slack.demand.list`` so demand signals are scoped"
        " to the period being summarised (Phase 18-C)"
    )


def test_personal_brief_description_advertises_slack_demand_signal() -> None:
    """The frontmatter description must mention the Slack demand signal.

    A user asking "今週どうなってる" only fires the skill if the
    description hints at it. ADR-0033 §決定 (c) pins
    ``personal-brief`` as one of the three target skills, so the
    description must advertise the new signal explicitly (or the
    host router collapses back to the pre-Phase-18 behaviour).
    """
    text = _read_skill()
    frontmatter, _body = parse_frontmatter(text)
    description = str(frontmatter.get("description", ""))
    markers = (
        "slack.demand.list",
        "Slack の @mention",
        "Slack demand",
        "demand 信号",
        "@mention / DM",
        "Phase 18",
    )
    hits = [m for m in markers if m in description]
    assert hits, (
        f"{_SKILL_PATH} description must advertise the Phase 18-C Slack"
        " demand signal (ADR-0033 §決定 (c)). Found nothing in"
        f" {markers!r}. Description: {description!r}"
    )


def test_personal_brief_keeps_baseline_period_summary_mcp_tools() -> None:
    """Phase 18-C must not drop the Phase 12 H1 baseline MCP tools.

    The period summary combines internal queue state (``task.list``
    + ``inbox.list`` + ``decision.list``) with semantic recall
    (``recall.search``) and optional LLM summary (``brief``). Phase
    18-C adds ``slack.demand.list`` as a 6th input. A regression
    that drops one of the original 5 would silently degrade the
    summary even though the slack.demand.list reference is intact.
    """
    text = _read_skill()
    for tool in ("brief", "recall.search", "task.list", "inbox.list", "decision.list"):
        assert tool in text, (
            f"{_SKILL_PATH} must keep the Phase 12 H1 baseline MCP tool"
            f" {tool!r} alongside the new Phase 18-C ``slack.demand.list``"
        )


def test_personal_brief_output_template_includes_slack_demand_section() -> None:
    """The output template must include a "Slack demand 信号" section.

    The Phase 18-C plan reserves a dedicated section in the rendered
    period summary so the operator can scan Slack ping context at
    a glance. The section marker must appear in the SKILL.md so a
    host's final render keeps the new input visible (otherwise the
    LLM might silently fold the Slack data into the recall section
    and lose the visual separation).
    """
    text = _read_skill()
    # Accept any of the canonical section markers the SKILL.md
    # could use; pin the presence of at least one.
    markers = (
        "Slack の demand 信号",
        "Slack で読むべき",
        "Slack mention",
        "Slack DM",
    )
    hits = [m for m in markers if m in text]
    assert hits, (
        f"{_SKILL_PATH} output template must include a Slack demand"
        " signal section so the host's render keeps it visible"
        f" (Phase 18-C). Found nothing in {markers!r}."
    )
