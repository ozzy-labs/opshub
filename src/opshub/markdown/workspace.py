"""On-disk driver for the disposable workspace tree (ADR-0003).

:func:`generate_workspace` reads the ``tasks`` projection from the supplied
:class:`~sqlalchemy.engine.Engine`, renders every row into markdown via
:func:`opshub.markdown.tasks.render_tasks_markdown`, and synchronises the
result with ``<workspace_root>/generated/tasks/``:

* New rendered files are created with their parent directories.
* Existing files are overwritten **only when content actually differs** —
  this keeps the second consecutive call a true no-op (no mtime churn,
  no spurious file-watcher events).
* Files present on disk but absent from the rendered mapping are
  deleted. This is the canonical "disposable workspace" semantics from
  ADR-0003: the workspace mirrors the read model, so a deleted task must
  not leave a stale ``.md`` orphan behind.

The function returns the count of files actually written (creations +
updates), which the CLI surfaces to the user. Deletions are not counted
in that number; they are an implementation detail of keeping the mirror
in sync.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.markdown.tasks import TaskRow, render_tasks_markdown
from opshub.projections import tasks_table

__all__ = ["generate_workspace"]


_GENERATED_SUBDIR = ("generated", "tasks")


def generate_workspace(engine: Engine, workspace_root: Path) -> int:
    """Regenerate the markdown workspace from the ``tasks`` projection.

    Parameters
    ----------
    engine:
        SQLAlchemy engine bound to a DB whose schema has already been
        migrated (the caller — typically ``opshub workspace generate`` —
        is responsible for ensuring ``opshub init`` ran first).
    workspace_root:
        Root of the workspace tree, as configured by
        :class:`~opshub.core.config.WorkspaceSettings`. Output is written
        under ``workspace_root / "generated" / "tasks"``.

    Returns
    -------
    int
        Number of files actually written. Calling this function twice on
        an unchanged projection returns ``0`` on the second call.
    """
    rows = _read_task_rows(engine)
    rendered = render_tasks_markdown(rows)

    target_dir = workspace_root.joinpath(*_GENERATED_SUBDIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    written = _sync_files(target_dir, rendered)
    _delete_orphans(target_dir, rendered)
    return written


def _read_task_rows(engine: Engine) -> list[TaskRow]:
    """Load every ``tasks`` row and adapt it into the markdown view-model.

    Datetime columns come back tz-naive on the stdlib sqlite3 driver
    (their components reflect UTC; see ``tests/integration/test_projections_rebuild``
    for the canonical note). The markdown layer doesn't care about
    tzinfo — templates only call ``isoformat()`` and ``date()`` — so we
    pass the values through unchanged.
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


def _sync_files(target_dir: Path, rendered: dict[str, str]) -> int:
    """Write rendered files, skipping ones whose content is already on disk."""
    written = 0
    for filename, content in rendered.items():
        path = target_dir / filename
        encoded = content.encode("utf-8")
        if path.is_file() and path.read_bytes() == encoded:
            continue
        path.write_bytes(encoded)
        written += 1
    return written


def _delete_orphans(target_dir: Path, rendered: dict[str, str]) -> None:
    """Remove ``*.md`` files under ``target_dir`` that the render didn't emit.

    Scoped to ``*.md`` so a future cohabitant (e.g. an ``index.json``
    alongside the markdown) wouldn't be clobbered. Hidden files are left
    alone for the same reason — editors and OS metadata are not ours to
    delete.
    """
    expected = set(rendered.keys())
    for entry in target_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        if entry.suffix != ".md":
            continue
        if entry.name in expected:
            continue
        entry.unlink()
