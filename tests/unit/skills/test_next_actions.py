"""Phase 18-C semantic pins for the ``next-actions`` SKILL.md SSOT.

ADR-0033 §決定 (c) wires the new ``slack.demand.list`` MCP tool into
``next-actions`` so the host can surface Slack ``<@self>`` mentions
and DM activity as "next to read" priority signals. These pins
capture the contract that:

* the SKILL.md body references ``slack.demand.list`` verbatim so the
  host router can dispatch through MCP without falling back to a
  CLI shell;
* the body uses the ``demand_kinds=["dm", "mention"]`` filter (the
  canonical Phase 18-C dispatch key — MPIM rows are not written by
  the Phase 18-B projection so excluding the kind here keeps the
  call surface minimal);
* the description / body advertise the demand signal so a user
  asking "今週やること" gets the Slack ping context, not just
  ``tasks`` (a regression that drops the wording would silently
  collapse the Phase 18-C UX gain);
* the dispatch list still includes ``task.list`` + ``recall.search``
  (Phase 12 H1 baseline) so the priority ranking has both the
  internal task queue and the external demand signal as inputs.

Snapshot-style: the tests pin the **literals** in the body rather
than driving the MCP handler — the handler is covered by
:mod:`tests.unit.mcp.test_slack_demand`. Decoupling the skill spec
pins from the handler keeps the SKILL.md drift detector cheap
(no engine fixture, no event store).
"""

from __future__ import annotations

from pathlib import Path

from tools.skill_scan import parse_frontmatter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_PATH = _REPO_ROOT / "docs" / "skills" / "next-actions" / "SKILL.md"


def _read_skill() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def test_next_actions_references_slack_demand_list_mcp_tool() -> None:
    """``next-actions`` SKILL.md must reference ``slack.demand.list`` literally.

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


def test_next_actions_uses_demand_kinds_filter_for_dm_and_mention() -> None:
    """The body must use the ``demand_kinds=["dm", "mention"]`` filter.

    Phase 18-B writes ``dm`` and ``mention`` rows but not ``mpim``
    (group-DM ``<@self>`` mentions land in the mention row per the
    projection module docstring). The Phase 18-C canonical call site
    therefore filters on the two emitted kinds — a body that omits
    the filter would also surface a future ``mpim`` row when Phase
    19+ starts writing them, which is fine for forward compatibility
    but loses the explicit "these are the operator-facing kinds I
    care about" signal the SKILL.md should carry today.
    """
    text = _read_skill()
    # Accept either the bracketed JSON form or a comma-separated
    # inline form (both are reasonable SKILL.md styles).
    has_dm_kind = "dm" in text and "mention" in text
    has_demand_kinds_marker = "demand_kinds" in text
    assert has_demand_kinds_marker and has_dm_kind, (
        f'{_SKILL_PATH} must use the ``demand_kinds=["dm", "mention"]``'
        " filter when calling ``slack.demand.list`` (Phase 18-C canonical"
        " dispatch key)"
    )


def test_next_actions_description_advertises_slack_demand_signal() -> None:
    """The frontmatter description must mention the Slack demand signal.

    A user asking "今週やること" / "優先度高いのは?" only fires the
    skill if the description hints at it. ADR-0033 §決定 (c) pins
    ``next-actions`` as one of the three target skills, so the
    description must advertise the new signal explicitly (or the
    host router collapses back to the pre-Phase-18 behaviour).
    """
    text = _read_skill()
    frontmatter, _body = parse_frontmatter(text)
    description = str(frontmatter.get("description", ""))
    # Accept a handful of canonical phrasings — the SKILL.md author
    # has stylistic freedom but at least one demand-signal marker
    # must appear.
    markers = (
        "slack.demand.list",
        "Slack の @mention",
        "@mention / DM",
        "demand 信号",
        "demand signal",
        "Phase 18",
    )
    hits = [m for m in markers if m in description]
    assert hits, (
        f"{_SKILL_PATH} description must advertise the Phase 18-C Slack"
        " demand signal (ADR-0033 §決定 (c)). Found nothing in"
        f" {markers!r}. Description: {description!r}"
    )


def test_next_actions_still_references_baseline_mcp_tools() -> None:
    """Phase 18-C must not drop the Phase 12 H1 ``task.list`` + ``recall.search`` baseline.

    The priority ranking combines the internal task queue
    (``task.list``) with the external demand signal
    (``slack.demand.list``). A regression that replaces one with the
    other would lose half of the input; this pin keeps both literals
    present so the skill remains the "consolidated next-action view"
    documented in Phase 12 H1.
    """
    text = _read_skill()
    for tool in ("task.list", "recall.search"):
        assert tool in text, (
            f"{_SKILL_PATH} must keep the Phase 12 H1 baseline MCP tool"
            f" {tool!r} alongside the new Phase 18-C ``slack.demand.list``"
        )


def test_next_actions_priority_snapshot_orders_demand_above_tasks() -> None:
    """Output snippet must surface Slack demand signals before generic tasks.

    The Phase 18-C plan in the SKILL.md positions Slack DMs / mentions
    as the **top** priority section (operator視点で「自分が放置している
    ping」が最優先 per ADR-0033 §決定 (c)). The body's output template
    must therefore list the Slack section ahead of the standard
    "今すぐ / 今日中 / 今週" task buckets so the host's final render
    keeps the priority order.

    The pin scans for the canonical section header and asserts it
    appears before the first ``## 今すぐ`` / ``## 今日中`` marker.
    """
    text = _read_skill()
    slack_marker = "Slack で読むべき"
    task_marker = "## 今すぐ"

    slack_idx = text.find(slack_marker)
    task_idx = text.find(task_marker)
    assert slack_idx != -1, (
        f"{_SKILL_PATH} output template must include a"
        f" {slack_marker!r} section (Phase 18-C priority snapshot)"
    )
    assert task_idx != -1, (
        f"{_SKILL_PATH} output template must keep the {task_marker!r} bucket (Phase 12 baseline)"
    )
    assert slack_idx < task_idx, (
        f"{_SKILL_PATH} output template must list the Slack demand"
        f" signal section ({slack_marker!r}) ABOVE the generic task"
        f" buckets ({task_marker!r}) — Phase 18-C priority snapshot"
        " (ADR-0033 §決定 (c) operator視点の「最優先 ping」順序)"
    )
