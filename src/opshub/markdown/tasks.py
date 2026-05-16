"""Render ``tasks`` projection rows into markdown files.

Two layers live here:

* :func:`render_tasks_markdown` is the pure renderer — it takes a list of
  :class:`TaskRow` view-models and returns a ``{filename: content}``
  mapping. Unit tests exercise this function directly against
  hand-crafted rows without spinning up a database.
* :class:`TasksRenderer` is the :class:`~opshub.markdown.workspace.WorkspaceRenderer`
  Protocol implementation. It reads from the ``tasks`` projection table
  via a SQLAlchemy :class:`~sqlalchemy.engine.Engine`, adapts each row
  into a :class:`TaskRow`, and delegates the rendering to
  :func:`render_tasks_markdown`. The workspace driver
  (:func:`opshub.markdown.workspace.generate_workspace`) iterates a list
  of renderers and dispatches through this Protocol.

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

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.markdown.render import env
from opshub.projections import tasks_table

__all__ = ["INDEX_FILENAME", "TaskRow", "TasksRenderer", "render_tasks_markdown"]


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


class TasksRenderer:
    """:class:`~opshub.markdown.workspace.WorkspaceRenderer` for ``tasks``.

    Reads the ``tasks`` projection, adapts each row into a
    :class:`TaskRow`, and delegates to :func:`render_tasks_markdown`.
    Writes into ``workspace_root / "generated" / "tasks" /``.
    """

    subdir: tuple[str, ...] = ("generated", "tasks")

    def read_and_render(self, engine: Engine) -> dict[str, str]:
        """Load every ``tasks`` row and render them to markdown."""
        rows = self._read_rows(engine)
        return render_tasks_markdown(rows)

    @staticmethod
    def _read_rows(engine: Engine) -> list[TaskRow]:
        """Load every ``tasks`` row and adapt it into the markdown view-model.

        Datetime columns come back tz-naive on the stdlib sqlite3 driver
        (their components reflect UTC; see
        ``tests/integration/test_projections_rebuild`` for the canonical
        note). The markdown layer doesn't care about tzinfo — templates
        only call ``isoformat()`` and ``date()`` — so we pass the values
        through unchanged.
        """
        stmt = select(tasks_table)
        with engine.connect() as conn:
            result = conn.execute(stmt).mappings().all()
        return [
            TaskRow(
                id=row["id"],
                title=row["title"],
                body=row["body"],
                state=row["state"],
                result_note=row["result_note"],
                created_at=_as_datetime(row["created_at"]),
                updated_at=_as_datetime(row["updated_at"]),
            )
            for row in result
        ]


def _as_datetime(value: object) -> datetime:
    """Narrow a SQLAlchemy column value to :class:`datetime`.

    The ``tasks`` table declares ``DateTime(timezone=True)`` columns and
    the SQLite driver returns ``datetime`` instances. We assert that
    contract here so a future schema regression (column type drift)
    surfaces with a useful error rather than a confusing template
    failure.
    """
    if not isinstance(value, datetime):  # pragma: no cover - defensive
        raise TypeError(f"expected datetime from tasks projection, got {type(value).__name__}")
    return value
