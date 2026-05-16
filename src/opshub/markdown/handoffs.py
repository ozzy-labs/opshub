"""Render the ``handoffs`` projection into markdown files.

Two layers, mirroring :mod:`opshub.markdown.tasks`:

* :func:`render_handoffs_markdown` is the pure renderer.
* :class:`HandoffsRenderer` implements the
  :class:`~opshub.markdown.workspace.WorkspaceRenderer` Protocol.

Output shape: one ``<handoff_id>.md`` per row + an ``index.md`` with two
sections ("Open" / "Closed"), each sorted by ``opened_at DESC, id ASC``.
Open handoffs surface first so a reader scanning the index sees what
still needs attention before the historical record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.markdown.render import env
from opshub.projections import handoffs_table

__all__ = [
    "HANDOFFS_INDEX_FILENAME",
    "HandoffRow",
    "HandoffsRenderer",
    "render_handoffs_markdown",
]


HANDOFFS_INDEX_FILENAME = "index.md"
"""Filename of the handoffs index in the rendered mapping."""

_STATE_OPEN = "open"
_STATE_CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class HandoffRow:
    """Presentation-side view of one ``handoffs`` row."""

    id: str
    from_actor: str
    to_actor: str
    topic: str
    state: str
    opened_at: datetime
    closed_at: datetime | None
    note: str | None


def render_handoffs_markdown(handoffs: list[HandoffRow]) -> dict[str, str]:
    """Render ``handoffs`` into a ``{filename: content}`` mapping.

    The mapping always contains :data:`HANDOFFS_INDEX_FILENAME` plus one
    ``<handoff.id>.md`` per row. The index has two sections, "Open"
    and "Closed", each sorted by ``(opened_at DESC, id ASC)`` — the
    lexicographic id tie-break keeps output stable when two handoffs
    are opened in the same millisecond.

    Rendering is deterministic: feeding the same list twice produces
    byte-identical strings — required by the workspace driver's
    idempotency contract.
    """
    environment = env()
    handoff_template = environment.get_template("handoff.md.j2")
    index_template = environment.get_template("handoff_index.md.j2")

    open_handoffs = sorted(
        (h for h in handoffs if h.state == _STATE_OPEN),
        key=lambda h: (-h.opened_at.timestamp(), h.id),
    )
    closed_handoffs = sorted(
        (h for h in handoffs if h.state == _STATE_CLOSED),
        key=lambda h: (-h.opened_at.timestamp(), h.id),
    )

    result: dict[str, str] = {
        HANDOFFS_INDEX_FILENAME: index_template.render(
            open_handoffs=open_handoffs,
            closed_handoffs=closed_handoffs,
        ),
    }
    for handoff in handoffs:
        result[f"{handoff.id}.md"] = handoff_template.render(handoff=handoff)
    return result


class HandoffsRenderer:
    """:class:`~opshub.markdown.workspace.WorkspaceRenderer` for ``handoffs``.

    Owns the ``workspace_root / "generated" / "handoffs" /`` subdirectory.
    """

    subdir: tuple[str, ...] = ("generated", "handoffs")

    def read_and_render(self, engine: Engine) -> dict[str, str]:
        """Load every ``handoffs`` row and render them to markdown."""
        rows = self._read_rows(engine)
        return render_handoffs_markdown(rows)

    @staticmethod
    def _read_rows(engine: Engine) -> list[HandoffRow]:
        """Load every ``handoffs`` row and adapt to the view-model."""
        stmt = select(handoffs_table)
        with engine.connect() as conn:
            result = conn.execute(stmt).mappings().all()
        return [
            HandoffRow(
                id=row["id"],
                from_actor=row["from_actor"],
                to_actor=row["to_actor"],
                topic=row["topic"],
                state=row["state"],
                opened_at=_as_datetime(row["opened_at"]),
                closed_at=_as_optional_datetime(row["closed_at"]),
                note=row["note"],
            )
            for row in result
        ]


def _as_datetime(value: object) -> datetime:
    """Narrow a SQLAlchemy column value to :class:`datetime`."""
    if not isinstance(value, datetime):  # pragma: no cover - defensive
        raise TypeError(f"expected datetime from handoffs projection, got {type(value).__name__}")
    return value


def _as_optional_datetime(value: object) -> datetime | None:
    """Narrow a nullable SQLAlchemy column value to ``datetime | None``."""
    if value is None:
        return None
    return _as_datetime(value)
