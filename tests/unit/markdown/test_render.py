"""Pure render tests for :func:`render_tasks_markdown`.

These tests deliberately stay db-free: they hand-craft :class:`TaskRow`
instances and only assert properties of the rendered string mapping. The
on-disk workspace driver is covered in
``tests/integration/test_workspace_generate.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opshub.markdown.tasks import INDEX_FILENAME, TaskRow, render_tasks_markdown


def _row(
    *,
    task_id: str,
    title: str = "demo",
    body: str | None = None,
    state: str = "draft",
    result_note: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> TaskRow:
    """Factory keeping each test's setup short and explicit."""
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    return TaskRow(
        id=task_id,
        title=title,
        body=body,
        state=state,
        result_note=result_note,
        created_at=created_at or base,
        updated_at=updated_at or base,
    )


def test_render_emits_index_and_one_file_per_task() -> None:
    rows = [
        _row(task_id="01HZZZZZZZZZZZZZZZZZZZZZ01"),
        _row(task_id="01HZZZZZZZZZZZZZZZZZZZZZ02"),
        _row(task_id="01HZZZZZZZZZZZZZZZZZZZZZ03"),
    ]
    rendered = render_tasks_markdown(rows)

    assert set(rendered.keys()) == {
        INDEX_FILENAME,
        "01HZZZZZZZZZZZZZZZZZZZZZ01.md",
        "01HZZZZZZZZZZZZZZZZZZZZZ02.md",
        "01HZZZZZZZZZZZZZZZZZZZZZ03.md",
    }


def test_render_is_byte_stable_across_calls() -> None:
    """Two renders of the same input must be byte-identical.

    This is the contract the workspace driver depends on to make a second
    consecutive ``generate_workspace`` call a true no-op.
    """
    rows = [
        _row(
            task_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            title="ship rendering",
            body="the body field",
            state="completed",
            result_note="merged in #16",
            updated_at=datetime(2026, 5, 17, 13, 0, 0, tzinfo=UTC),
        ),
        _row(
            task_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            title="other task",
            state="active",
            updated_at=datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC),
        ),
    ]

    first = render_tasks_markdown(rows)
    second = render_tasks_markdown(rows)

    assert first == second
    # Be explicit: bytes comparison rules out unicode normalisation drift.
    for filename, content in first.items():
        assert content.encode("utf-8") == second[filename].encode("utf-8")


def test_index_contains_expected_row_count_and_link_per_task() -> None:
    rows = [
        _row(task_id="01HZZZZZZZZZZZZZZZZZZZZZ01", title="alpha"),
        _row(task_id="01HZZZZZZZZZZZZZZZZZZZZZ02", title="beta"),
    ]
    index = render_tasks_markdown(rows)[INDEX_FILENAME]

    # One header row + one separator row + N task rows.
    table_rows = [line for line in index.splitlines() if line.startswith("|")]
    assert len(table_rows) == 2 + len(rows)
    # Each task id appears in a relative markdown link.
    for row in rows:
        assert f"./{row.id}.md" in index


def test_index_renders_placeholder_when_no_tasks() -> None:
    rendered = render_tasks_markdown([])
    index = rendered[INDEX_FILENAME]

    assert set(rendered.keys()) == {INDEX_FILENAME}
    assert "No tasks yet" in index
    # No table chrome leaks out.
    assert "| ID |" not in index


def test_per_task_body_and_result_note_are_optional() -> None:
    plain = _row(task_id="01HZZZZZZZZZZZZZZZZZZZZZ10")
    rich = _row(
        task_id="01HZZZZZZZZZZZZZZZZZZZZZ11",
        body="some body text",
        state="completed",
        result_note="all done",
    )
    rendered = render_tasks_markdown([plain, rich])

    plain_md = rendered[f"{plain.id}.md"]
    rich_md = rendered[f"{rich.id}.md"]

    # ``# title`` always present; optional sections only when populated.
    assert plain_md.startswith(f"# {plain.title}\n")
    assert "## Body" not in plain_md
    assert "## Result note" not in plain_md

    assert "## Body" in rich_md
    assert "some body text" in rich_md
    assert "## Result note" in rich_md
    assert "all done" in rich_md


def test_index_is_sorted_by_updated_at_desc_then_id_asc() -> None:
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    rows = [
        _row(
            task_id="01HZZZZZZZZZZZZZZZZZZZZZ02",
            title="oldest",
            updated_at=base,
        ),
        _row(
            task_id="01HZZZZZZZZZZZZZZZZZZZZZ03",
            title="newest",
            updated_at=base + timedelta(hours=2),
        ),
        _row(
            task_id="01HZZZZZZZZZZZZZZZZZZZZZ01",
            title="middle-by-id",
            updated_at=base + timedelta(hours=1),
        ),
        _row(
            task_id="01HZZZZZZZZZZZZZZZZZZZZZ04",
            title="middle-by-tie",
            updated_at=base + timedelta(hours=1),
        ),
    ]
    index = render_tasks_markdown(rows)[INDEX_FILENAME]
    body_rows = [
        line
        for line in index.splitlines()
        if line.startswith("|") and not line.startswith("| ---") and "| ID |" not in line
    ]

    # Expected order: newest first; on tie at +1h, lexicographic id wins.
    titles_in_order = ["newest", "middle-by-id", "middle-by-tie", "oldest"]
    for row, expected in zip(body_rows, titles_in_order, strict=True):
        assert expected in row
