"""Render the ``inbox_items`` projection into markdown files.

Two layers, mirroring :mod:`opshub.markdown.tasks`:

* :func:`render_inbox_markdown` is the pure renderer — pass a list of
  :class:`InboxItemRow` values, get back a ``{filename: content}``
  mapping. Unit tests exercise this without touching a database.
* :class:`InboxRenderer` is the
  :class:`~opshub.markdown.workspace.WorkspaceRenderer` Protocol
  implementation. It reads the ``inbox_items`` projection and adapts
  each row into an :class:`InboxItemRow`.

Output shape: one ``<item_id>.md`` per inbox row, plus an ``index.md``
that groups every item by ``state``. The four states
(``pending`` / ``triaged_to_task`` / ``triaged_to_decision`` /
``discarded``) each get a section in the index; empty sections render
a placeholder line so a reader can tell "there are no pending items"
apart from "the section was never generated".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.markdown.render import env
from opshub.projections import inbox_items_table

__all__ = [
    "INBOX_INDEX_FILENAME",
    "INBOX_STATES",
    "InboxItemRow",
    "InboxRenderer",
    "render_inbox_markdown",
]


INBOX_INDEX_FILENAME = "index.md"
"""Filename of the inbox index in the rendered mapping."""

INBOX_STATES: tuple[str, ...] = (
    "pending",
    "triaged_to_task",
    "triaged_to_decision",
    "discarded",
)
"""Canonical render order for the per-state sections in the index.

Pinned to mirror :data:`opshub.projections.inbox._DISPOSITION_TO_STATE`'s
target literals plus the seed ``pending`` state. Order is deliberately
"workflow forward" — ``pending`` first (newest, most actionable),
``discarded`` last (terminal, least interesting).
"""


@dataclass(frozen=True, slots=True)
class InboxItemRow:
    """Presentation-side view of one ``inbox_items`` row."""

    id: str
    summary: str
    source_ref: str | None
    state: str
    disposition: str | None
    target_id: str | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


def render_inbox_markdown(items: list[InboxItemRow]) -> dict[str, str]:
    """Render ``items`` into a ``{filename: content}`` mapping.

    The mapping always contains :data:`INBOX_INDEX_FILENAME` plus one
    ``<item.id>.md`` per row. Per-state index sections are grouped by
    :data:`INBOX_STATES` (in canonical order) and sorted by
    ``(updated_at DESC, id ASC)`` within each section.

    Rendering is deterministic: feeding the same list twice produces
    byte-identical strings — the property the workspace driver depends
    on for idempotent regeneration.
    """
    environment = env()
    item_template = environment.get_template("inbox_item.md.j2")
    index_template = environment.get_template("inbox_index.md.j2")

    grouped: dict[str, list[InboxItemRow]] = {state: [] for state in INBOX_STATES}
    for item in items:
        # Defensive: an unknown state would be a CHECK-constraint
        # violation upstream, but we tolerate it gracefully by
        # silently dropping the row from the index (the per-item .md
        # is still rendered, so the data isn't lost).
        if item.state in grouped:
            grouped[item.state].append(item)

    for state in INBOX_STATES:
        grouped[state].sort(key=lambda r: (-r.updated_at.timestamp(), r.id))

    result: dict[str, str] = {
        INBOX_INDEX_FILENAME: index_template.render(
            states=INBOX_STATES,
            grouped=grouped,
        ),
    }
    for item in items:
        result[f"{item.id}.md"] = item_template.render(item=item)
    return result


class InboxRenderer:
    """:class:`~opshub.markdown.workspace.WorkspaceRenderer` for ``inbox_items``.

    Owns the ``workspace_root / "generated" / "inbox" /`` subdirectory.
    """

    subdir: tuple[str, ...] = ("generated", "inbox")

    def read_and_render(self, engine: Engine) -> dict[str, str]:
        """Load every ``inbox_items`` row and render them to markdown."""
        rows = self._read_rows(engine)
        return render_inbox_markdown(rows)

    @staticmethod
    def _read_rows(engine: Engine) -> list[InboxItemRow]:
        """Load every ``inbox_items`` row and adapt to the view-model."""
        stmt = select(inbox_items_table)
        with engine.connect() as conn:
            result = conn.execute(stmt).mappings().all()
        return [
            InboxItemRow(
                id=row["id"],
                summary=row["summary"],
                source_ref=row["source_ref"],
                state=row["state"],
                disposition=row["disposition"],
                target_id=row["target_id"],
                reason=row["reason"],
                created_at=_as_datetime(row["created_at"]),
                updated_at=_as_datetime(row["updated_at"]),
            )
            for row in result
        ]


def _as_datetime(value: object) -> datetime:
    """Narrow a SQLAlchemy column value to :class:`datetime`.

    Mirrors the helper in :mod:`opshub.markdown.tasks` — kept module-local
    so each renderer is self-contained and the markdown layer stays free
    of cross-module helper imports.
    """
    if not isinstance(value, datetime):  # pragma: no cover - defensive
        raise TypeError(f"expected datetime from inbox projection, got {type(value).__name__}")
    return value
