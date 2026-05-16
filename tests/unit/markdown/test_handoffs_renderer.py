"""Pure render tests for :func:`render_handoffs_markdown`.

DB-free: rows are hand-crafted :class:`HandoffRow` instances.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opshub.markdown.handoffs import (
    HANDOFFS_INDEX_FILENAME,
    HandoffRow,
    render_handoffs_markdown,
)


def _row(
    *,
    handoff_id: str,
    from_actor: str = "alice",
    to_actor: str = "bob",
    topic: str = "rotate creds",
    state: str = "open",
    opened_at: datetime | None = None,
    closed_at: datetime | None = None,
    note: str | None = None,
) -> HandoffRow:
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    return HandoffRow(
        id=handoff_id,
        from_actor=from_actor,
        to_actor=to_actor,
        topic=topic,
        state=state,
        opened_at=opened_at or base,
        closed_at=closed_at,
        note=note,
    )


def test_render_emits_index_and_one_file_per_handoff() -> None:
    rows = [
        _row(handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ01"),
        _row(
            handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            state="closed",
            closed_at=datetime(2026, 5, 17, 13, 0, 0, tzinfo=UTC),
            note="handed off cleanly",
        ),
    ]
    rendered = render_handoffs_markdown(rows)

    assert set(rendered.keys()) == {
        HANDOFFS_INDEX_FILENAME,
        "01HZZZZZZZZZZZZZZZZZZZZZ01.md",
        "01HZZZZZZZZZZZZZZZZZZZZZ02.md",
    }


def test_render_is_byte_stable_across_calls() -> None:
    rows = [
        _row(
            handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            topic="rotate db creds",
            opened_at=datetime(2026, 5, 17, 13, 0, 0, tzinfo=UTC),
        ),
        _row(
            handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            from_actor="carol",
            to_actor="dave",
            topic="finalise deploy",
            state="closed",
            opened_at=datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC),
            closed_at=datetime(2026, 5, 17, 15, 0, 0, tzinfo=UTC),
            note="merged and deployed",
        ),
    ]

    first = render_handoffs_markdown(rows)
    second = render_handoffs_markdown(rows)

    assert first == second
    for filename, content in first.items():
        assert content.encode("utf-8") == second[filename].encode("utf-8")


def test_index_renders_both_sections_even_when_empty() -> None:
    rendered = render_handoffs_markdown([])
    index = rendered[HANDOFFS_INDEX_FILENAME]

    assert set(rendered.keys()) == {HANDOFFS_INDEX_FILENAME}
    assert "## Open" in index
    assert "## Closed" in index
    assert "_No open handoffs._" in index
    assert "_No closed handoffs._" in index
    assert "| ID |" not in index


def test_index_splits_open_and_closed_into_separate_sections() -> None:
    rows = [
        _row(handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ01", topic="open-topic", state="open"),
        _row(
            handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            topic="closed-topic",
            state="closed",
            closed_at=datetime(2026, 5, 17, 15, 0, 0, tzinfo=UTC),
        ),
    ]
    index = render_handoffs_markdown(rows)[HANDOFFS_INDEX_FILENAME]

    open_block, closed_block = index.split("## Closed", 1)
    assert "open-topic" in open_block
    assert "closed-topic" not in open_block
    assert "closed-topic" in closed_block
    assert "open-topic" not in closed_block


def test_index_sorts_each_section_by_opened_at_desc_then_id_asc() -> None:
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    rows = [
        _row(handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ02", topic="oldest", opened_at=base),
        _row(
            handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ03",
            topic="newest",
            opened_at=base + timedelta(hours=2),
        ),
        _row(
            handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            topic="middle-by-id",
            opened_at=base + timedelta(hours=1),
        ),
        _row(
            handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ04",
            topic="middle-by-tie",
            opened_at=base + timedelta(hours=1),
        ),
    ]
    index = render_handoffs_markdown(rows)[HANDOFFS_INDEX_FILENAME]
    open_block = index.split("## Open", 1)[1].split("## Closed", 1)[0]
    table_rows = [line for line in open_block.splitlines() if line.startswith("| [")]
    topics_in_order = ["newest", "middle-by-id", "middle-by-tie", "oldest"]
    for line, expected in zip(table_rows, topics_in_order, strict=True):
        assert expected in line


def test_per_handoff_close_fields_are_optional_when_open() -> None:
    open_row = _row(handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ10")
    closed_row = _row(
        handoff_id="01HZZZZZZZZZZZZZZZZZZZZZ11",
        state="closed",
        closed_at=datetime(2026, 5, 17, 15, 0, 0, tzinfo=UTC),
        note="handed off cleanly",
    )
    rendered = render_handoffs_markdown([open_row, closed_row])

    open_md = rendered[f"{open_row.id}.md"]
    closed_md = rendered[f"{closed_row.id}.md"]

    assert "**Closed:**" not in open_md
    assert "## Note" not in open_md

    assert "**Closed:** 2026-05-17T15:00:00+00:00" in closed_md
    assert "## Note" in closed_md
    assert "handed off cleanly" in closed_md
