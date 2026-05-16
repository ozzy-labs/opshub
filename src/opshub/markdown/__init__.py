"""Markdown rendering of the OpsHub read-model into a disposable workspace.

ADR-0003 designates the workspace tree as a *fully regenerable* mirror of
the read-model projections: nothing under ``workspace/generated/`` is
considered a source of truth, and the entire subtree can be deleted and
rebuilt from the event log at any time.

This package owns the rendering pipeline:

1. :mod:`opshub.markdown.render` provides the Jinja2 ``Environment``
   factory. Templates are loaded via ``PackageLoader`` so they ship in the
   built wheel rather than being read off the source checkout.
2. :mod:`opshub.markdown.tasks` turns ``tasks`` projection rows into a
   filename → content mapping via :func:`render_tasks_markdown`.
3. :mod:`opshub.markdown.workspace` is the on-disk driver: it reads the
   projection, renders, writes only changed files, and deletes orphans so
   the workspace stays a faithful mirror.

Dependencies are restricted to ``opshub.core``, ``opshub.db.schema`` /
``opshub.projections`` (for ``tasks_table``), and Jinja2 / SQLAlchemy —
``markdown/`` must never import from ``opshub.services`` or
``opshub.domain`` (ADR-0004 one-way dependency direction).
"""

from __future__ import annotations

from opshub.markdown.tasks import TaskRow, render_tasks_markdown
from opshub.markdown.workspace import generate_workspace

__all__ = [
    "TaskRow",
    "generate_workspace",
    "render_tasks_markdown",
]
