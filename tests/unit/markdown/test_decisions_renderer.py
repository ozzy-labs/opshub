"""Pure render tests for :func:`render_decisions_markdown`.

DB-free: rows are hand-crafted :class:`DecisionRow` instances.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opshub.markdown.decisions import (
    DECISIONS_INDEX_FILENAME,
    DecisionRow,
    render_decisions_markdown,
)


def _row(
    *,
    decision_id: str,
    text: str = "adopt the proposal",
    context: str | None = None,
    actor: str = "cli:default",
    recorded_at: datetime | None = None,
) -> DecisionRow:
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    return DecisionRow(
        id=decision_id,
        text=text,
        context=context,
        actor=actor,
        recorded_at=recorded_at or base,
    )


def test_render_emits_index_and_one_file_per_decision() -> None:
    rows = [
        _row(decision_id="01HZZZZZZZZZZZZZZZZZZZZZ01"),
        _row(decision_id="01HZZZZZZZZZZZZZZZZZZZZZ02"),
    ]
    rendered = render_decisions_markdown(rows)

    assert set(rendered.keys()) == {
        DECISIONS_INDEX_FILENAME,
        "01HZZZZZZZZZZZZZZZZZZZZZ01.md",
        "01HZZZZZZZZZZZZZZZZZZZZZ02.md",
    }


def test_render_is_byte_stable_across_calls() -> None:
    rows = [
        _row(
            decision_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            text="ship the rendering layer\nin step 8",
            context="phase 2 plan §2.3",
            actor="cli:claude",
            recorded_at=datetime(2026, 5, 17, 13, 0, 0, tzinfo=UTC),
        ),
        _row(
            decision_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            text="defer ML deps",
            recorded_at=datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC),
        ),
    ]

    first = render_decisions_markdown(rows)
    second = render_decisions_markdown(rows)

    assert first == second
    for filename, content in first.items():
        assert content.encode("utf-8") == second[filename].encode("utf-8")


def test_index_renders_placeholder_when_no_decisions() -> None:
    rendered = render_decisions_markdown([])
    index = rendered[DECISIONS_INDEX_FILENAME]

    assert set(rendered.keys()) == {DECISIONS_INDEX_FILENAME}
    assert "No decisions recorded yet" in index
    assert "| ID |" not in index


def test_index_is_sorted_by_recorded_at_desc_then_id_asc() -> None:
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    rows = [
        _row(decision_id="01HZZZZZZZZZZZZZZZZZZZZZ02", text="oldest", recorded_at=base),
        _row(
            decision_id="01HZZZZZZZZZZZZZZZZZZZZZ03",
            text="newest",
            recorded_at=base + timedelta(hours=2),
        ),
        _row(
            decision_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            text="middle-by-id",
            recorded_at=base + timedelta(hours=1),
        ),
        _row(
            decision_id="01HZZZZZZZZZZZZZZZZZZZZZ04",
            text="middle-by-tie",
            recorded_at=base + timedelta(hours=1),
        ),
    ]
    index = render_decisions_markdown(rows)[DECISIONS_INDEX_FILENAME]
    body_rows = [
        line
        for line in index.splitlines()
        if line.startswith("|") and not line.startswith("| ---") and "| ID |" not in line
    ]
    titles_in_order = ["newest", "middle-by-id", "middle-by-tie", "oldest"]
    for line, expected in zip(body_rows, titles_in_order, strict=True):
        assert expected in line


def test_per_decision_context_is_optional() -> None:
    plain = _row(decision_id="01HZZZZZZZZZZZZZZZZZZZZZ10")
    rich = _row(
        decision_id="01HZZZZZZZZZZZZZZZZZZZZZ11",
        context="cross-team agreement after RFC-42",
    )
    rendered = render_decisions_markdown([plain, rich])

    plain_md = rendered[f"{plain.id}.md"]
    rich_md = rendered[f"{rich.id}.md"]

    assert "## Decision" in plain_md
    assert "## Context" not in plain_md

    assert "## Context" in rich_md
    assert "cross-team agreement after RFC-42" in rich_md


def test_index_uses_first_line_of_text_for_multi_line_decisions() -> None:
    """Multi-line decision text collapses to its first line in the index."""
    rows = [
        _row(
            decision_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            text="adopt new convention\nrationale follows here",
        ),
    ]
    index = render_decisions_markdown(rows)[DECISIONS_INDEX_FILENAME]
    body_rows = [line for line in index.splitlines() if line.startswith("| [")]
    assert len(body_rows) == 1
    assert "adopt new convention" in body_rows[0]
    # The rationale prose must not leak into the table cell.
    assert "rationale follows here" not in body_rows[0]
