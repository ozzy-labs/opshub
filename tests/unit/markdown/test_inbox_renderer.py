"""Pure render tests for :func:`render_inbox_markdown`.

DB-free: rows are hand-crafted :class:`InboxItemRow` instances. The
on-disk workspace driver and the
:meth:`InboxRenderer.read_and_render` adapter are covered in
``tests/integration/test_workspace_generate.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opshub.markdown.inbox import (
    INBOX_INDEX_FILENAME,
    INBOX_STATES,
    InboxItemRow,
    render_inbox_markdown,
)


def _row(
    *,
    item_id: str,
    summary: str = "demo summary",
    source_ref: str | None = None,
    state: str = "pending",
    disposition: str | None = None,
    target_id: str | None = None,
    reason: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> InboxItemRow:
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    return InboxItemRow(
        id=item_id,
        summary=summary,
        source_ref=source_ref,
        state=state,
        disposition=disposition,
        target_id=target_id,
        reason=reason,
        created_at=created_at or base,
        updated_at=updated_at or base,
    )


def test_render_emits_index_and_one_file_per_item() -> None:
    rows = [
        _row(item_id="01HZZZZZZZZZZZZZZZZZZZZZ01"),
        _row(item_id="01HZZZZZZZZZZZZZZZZZZZZZ02", state="triaged_to_task"),
        _row(item_id="01HZZZZZZZZZZZZZZZZZZZZZ03", state="discarded"),
    ]
    rendered = render_inbox_markdown(rows)

    assert set(rendered.keys()) == {
        INBOX_INDEX_FILENAME,
        "01HZZZZZZZZZZZZZZZZZZZZZ01.md",
        "01HZZZZZZZZZZZZZZZZZZZZZ02.md",
        "01HZZZZZZZZZZZZZZZZZZZZZ03.md",
    }


def test_render_is_byte_stable_across_calls() -> None:
    """Two renders of the same input must be byte-identical."""
    rows = [
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            summary="capture issue",
            source_ref="github:foo/bar#42",
            state="triaged_to_task",
            disposition="to_task",
            target_id="01HZZZZZZZZZZZZZZZZZZZZTASK",
            updated_at=datetime(2026, 5, 17, 13, 0, 0, tzinfo=UTC),
        ),
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            summary="unclear request",
            state="discarded",
            disposition="discard",
            reason="duplicate of #41",
            updated_at=datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC),
        ),
    ]

    first = render_inbox_markdown(rows)
    second = render_inbox_markdown(rows)

    assert first == second
    for filename, content in first.items():
        assert content.encode("utf-8") == second[filename].encode("utf-8")


def test_index_renders_section_per_state_even_when_empty() -> None:
    rendered = render_inbox_markdown([])
    index = rendered[INBOX_INDEX_FILENAME]

    assert set(rendered.keys()) == {INBOX_INDEX_FILENAME}
    for state in INBOX_STATES:
        assert f"## {state}" in index
    # Empty sections render the placeholder line, not a stray table.
    assert "_No items in this state._" in index
    assert "| ID |" not in index


def test_index_groups_items_by_state() -> None:
    rows = [
        _row(item_id="01HZZZZZZZZZZZZZZZZZZZZZ01", summary="alpha", state="pending"),
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            summary="beta",
            state="triaged_to_task",
            disposition="to_task",
            target_id="01HZZZZZZZZZZZZZZZZZZZZTSK",
        ),
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ03",
            summary="gamma",
            state="discarded",
            disposition="discard",
            reason="not actionable",
        ),
    ]
    index = render_inbox_markdown(rows)[INBOX_INDEX_FILENAME]

    # Each row appears under its own section, not in any other.
    pending_block = index.split("## pending", 1)[1].split("## ", 1)[0]
    assert "alpha" in pending_block
    assert "beta" not in pending_block
    assert "gamma" not in pending_block

    discarded_block = index.split("## discarded", 1)[1]
    assert "gamma" in discarded_block
    assert "alpha" not in discarded_block


def test_index_is_sorted_within_state_by_updated_desc_then_id_asc() -> None:
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    rows = [
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            summary="oldest",
            updated_at=base,
        ),
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ03",
            summary="newest",
            updated_at=base + timedelta(hours=2),
        ),
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            summary="middle-by-id",
            updated_at=base + timedelta(hours=1),
        ),
        _row(
            item_id="01HZZZZZZZZZZZZZZZZZZZZZ04",
            summary="middle-by-tie",
            updated_at=base + timedelta(hours=1),
        ),
    ]
    index = render_inbox_markdown(rows)[INBOX_INDEX_FILENAME]
    pending_block = index.split("## pending", 1)[1].split("## ", 1)[0]
    table_rows = [line for line in pending_block.splitlines() if line.startswith("| [")]
    summaries_in_order = ["newest", "middle-by-id", "middle-by-tie", "oldest"]
    for line, expected in zip(table_rows, summaries_in_order, strict=True):
        assert expected in line


def test_per_item_optional_fields_are_omitted_when_absent() -> None:
    plain = _row(item_id="01HZZZZZZZZZZZZZZZZZZZZZ10")
    rich = _row(
        item_id="01HZZZZZZZZZZZZZZZZZZZZZ11",
        source_ref="slack:#ops/123",
        state="triaged_to_task",
        disposition="to_task",
        target_id="01HZZZZZZZZZZZZZZZZZZZZTSK",
        reason="promoted from triage",
    )
    rendered = render_inbox_markdown([plain, rich])

    plain_md = rendered[f"{plain.id}.md"]
    rich_md = rendered[f"{rich.id}.md"]

    assert "**Source:**" not in plain_md
    assert "**Disposition:**" not in plain_md
    assert "**Target:**" not in plain_md
    assert "**Reason:**" not in plain_md

    assert "**Source:** slack:#ops/123" in rich_md
    assert "**Disposition:** to_task" in rich_md
    assert "**Target:** `01HZZZZZZZZZZZZZZZZZZZZTSK`" in rich_md
    assert "**Reason:** promoted from triage" in rich_md
