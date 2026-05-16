"""On-disk driver for the disposable workspace tree (ADR-0003).

:func:`generate_workspace` iterates a list of
:class:`WorkspaceRenderer` implementations, hands each one a SQLAlchemy
:class:`~sqlalchemy.engine.Engine`, and synchronises the
``{filename: content}`` mapping each renderer returns with the
corresponding subdirectory under ``workspace_root``:

* New rendered files are created with their parent directories.
* Existing files are overwritten **only when content actually differs** —
  this keeps the second consecutive call a true no-op (no mtime churn,
  no spurious file-watcher events).
* Files present on disk but absent from the rendered mapping for a given
  renderer are deleted **inside that renderer's subdir only**. This is
  the canonical "disposable workspace" semantics from ADR-0003: each
  subdir mirrors exactly the read-model slice that owns it, so a deleted
  row must not leave a stale ``.md`` orphan behind. Crucially, deletion
  is scoped per renderer so each renderer can be developed in isolation
  without one renderer's output accidentally clobbering another's.

Writes are atomic (M2 in plan §2.3 step 8): each file is first written
to a sibling ``*.tmp`` path and then ``os.replace``-d into place. On
POSIX this guarantees concurrent readers (agents using the Read tool,
file-watchers, ``opshub`` subprocesses) see either the previous version
or the new one — never a half-written file.

The function returns the total count of files actually written across
every renderer (creations + updates). Deletions are not counted in that
number; they are an implementation detail of keeping each mirror in
sync.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from sqlalchemy.engine import Engine

from opshub.markdown.decisions import DecisionsRenderer
from opshub.markdown.handoffs import HandoffsRenderer
from opshub.markdown.inbox import InboxRenderer
from opshub.markdown.tasks import TasksRenderer

__all__ = ["WorkspaceRenderer", "generate_workspace"]


@runtime_checkable
class WorkspaceRenderer(Protocol):
    """Render a projection slice into a ``{filename: content}`` mapping.

    Each renderer owns one subdirectory under ``workspace_root``.
    The mapping returned by :meth:`read_and_render` becomes the
    authoritative set of files in that subdir; orphan ``.md`` files
    (present on disk, absent from the mapping) are deleted.

    Attributes
    ----------
    subdir:
        Path components, relative to ``workspace_root``, where this
        renderer's files live (e.g. ``("generated", "tasks")``).
    """

    subdir: tuple[str, ...]

    def read_and_render(self, engine: Engine) -> dict[str, str]:
        """Return a ``{filename: content}`` mapping for this renderer's subdir."""
        ...


def _default_renderers() -> list[WorkspaceRenderer]:
    """Return the canonical renderer list used by :func:`generate_workspace`.

    Isolated as a function (rather than a module-level constant) so that
    each call constructs fresh renderer instances. The renderers are
    stateless today, but keeping a fresh-per-call shape leaves room for
    a future renderer to cache template lookups without leaking state
    between :func:`generate_workspace` invocations.
    """
    return [
        TasksRenderer(),
        InboxRenderer(),
        DecisionsRenderer(),
        HandoffsRenderer(),
    ]


def generate_workspace(engine: Engine, workspace_root: Path) -> int:
    """Regenerate the markdown workspace from every registered renderer.

    Parameters
    ----------
    engine:
        SQLAlchemy engine bound to a DB whose schema has already been
        migrated (the caller — typically ``opshub workspace generate`` —
        is responsible for ensuring ``opshub init`` ran first).
    workspace_root:
        Root of the workspace tree, as configured by
        :class:`~opshub.core.config.WorkspaceSettings`. Each renderer
        writes under ``workspace_root / *renderer.subdir``.

    Returns
    -------
    int
        Total number of files actually written across every renderer.
        Calling this function twice on an unchanged projection returns
        ``0`` on the second call.
    """
    total_written = 0
    for renderer in _default_renderers():
        rendered = renderer.read_and_render(engine)

        target_dir = workspace_root.joinpath(*renderer.subdir)
        target_dir.mkdir(parents=True, exist_ok=True)

        total_written += _sync_files(target_dir, rendered)
        _delete_orphans(target_dir, rendered)
    return total_written


def _sync_files(target_dir: Path, rendered: dict[str, str]) -> int:
    """Write rendered files atomically, skipping bytes-equal no-ops.

    For each ``(filename, content)`` pair this function:

    1. Skips the write if ``target_dir / filename`` already exists with
       byte-identical contents. This is what makes a second consecutive
       :func:`generate_workspace` call a true no-op (no mtime churn).
    2. Otherwise, writes the new bytes to a sibling ``*.tmp`` path and
       ``os.replace``-s it into the final location. ``os.replace`` is
       atomic on POSIX, so concurrent readers (agents using the Read
       tool, file-watchers) see either the old version or the new
       version, never a half-written file.

    The ``*.tmp`` path is removed on a failed write so a crash mid-flight
    does not leave stale temporary files alongside the canonical output.
    """
    written = 0
    for filename, content in rendered.items():
        path = target_dir / filename
        encoded = content.encode("utf-8")
        if path.is_file() and path.read_bytes() == encoded:
            continue
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(encoded)
            os.replace(tmp_path, path)
        except BaseException:
            # Best-effort cleanup of the partial temp file. We re-raise
            # the original exception so the caller still sees the
            # failure — this branch only exists to avoid littering the
            # workspace with ``*.md.tmp`` orphans after an interrupted
            # write (e.g. KeyboardInterrupt, disk-full).
            tmp_path.unlink(missing_ok=True)
            raise
        written += 1
    return written


def _delete_orphans(target_dir: Path, rendered: dict[str, str]) -> None:
    """Remove ``*.md`` files under ``target_dir`` that the render didn't emit.

    Scoped to ``*.md`` so a future cohabitant (e.g. an ``index.json``
    alongside the markdown) wouldn't be clobbered. Hidden files are left
    alone for the same reason — editors and OS metadata are not ours to
    delete. Deletion runs **per renderer subdir** so each renderer's
    cleanup is independent of every other renderer's output.
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
