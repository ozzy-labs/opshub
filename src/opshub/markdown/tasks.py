"""Render ``tasks`` projection rows into markdown files.

The public surface is :func:`render_tasks_markdown`, which takes a list of
:class:`TaskRow` values and returns a ``{filename: content}`` mapping
suitable for handing to :func:`opshub.markdown.workspace.generate_workspace`.

:class:`TaskRow` is a slim, presentation-only dataclass that mirrors the
columns of :data:`opshub.projections.tasks.tasks_table`. We deliberately
keep it independent from the SQLAlchemy ``Row`` type so:

* template authors get attribute access (``task.title``) rather than
  string-keyed dict access (``task["title"]``);
* templates can be unit-tested without spinning up a database; and
* the rendering layer never imports from ``opshub.services`` or
  ``opshub.domain`` (ADR-0004 one-way dependency rule).

The index file is rendered with tasks sorted by ``updated_at DESC, id ASC``
so the most recently touched work surfaces at the top while the secondary
``id`` key keeps the order stable when timestamps tie (relevant for
back-to-back test inserts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from opshub.markdown.render import env

__all__ = ["INDEX_FILENAME", "TaskRow", "render_tasks_markdown"]


INDEX_FILENAME = "index.md"
"""Filename used for the workspace index in the rendered mapping."""


@dataclass(frozen=True, slots=True)
class TaskRow:
    """Presentation-side view of a single ``tasks`` projection row.

    Mirrors the columns of :data:`opshub.projections.tasks.tasks_table` but
    intentionally lives in the markdown layer so templates never depend on
    SQLAlchemy types. ``frozen=True`` makes rows hashable + immutable so
    tests can compare snapshots cheaply.
    """

    id: str
    title: str
    body: str | None
    state: str
    result_note: str | None
    created_at: datetime
    updated_at: datetime


def render_tasks_markdown(tasks: list[TaskRow]) -> dict[str, str]:
    """Render ``tasks`` into a ``{filename: content}`` mapping.

    The returned mapping always contains an entry keyed by
    :data:`INDEX_FILENAME` (the index page) plus one ``<task.id>.md`` entry
    per row. The index lists tasks sorted by ``(updated_at DESC, id ASC)``.

    Rendering is deterministic and side-effect free: calling twice with the
    same input must produce byte-identical strings (this is the property
    the workspace driver depends on to keep the second regeneration a
    no-op).
    """
    environment = env()
    task_template = environment.get_template("tasks.md.j2")
    index_template = environment.get_template("index.md.j2")

    # Sort for the index only: the per-task file mapping is keyed by id,
    # so its iteration order doesn't matter for output.
    index_tasks = sorted(tasks, key=lambda t: (-t.updated_at.timestamp(), t.id))

    result: dict[str, str] = {
        INDEX_FILENAME: index_template.render(tasks=index_tasks),
    }
    for task in tasks:
        result[f"{task.id}.md"] = task_template.render(task=task)
    return result
