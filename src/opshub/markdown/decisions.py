"""Render the ``decisions`` projection into markdown files.

Two layers, mirroring :mod:`opshub.markdown.tasks`:

* :func:`render_decisions_markdown` is the pure renderer.
* :class:`DecisionsRenderer` implements the
  :class:`~opshub.markdown.workspace.WorkspaceRenderer` Protocol.

Output shape: one ``<decision_id>.md`` per row + an ``index.md`` table
sorted by ``recorded_at DESC, id ASC`` so the most recent decision
surfaces first.

Decisions are append-only (Phase 2 has no edit/supersede transitions),
so the renderer doesn't need to model state at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.markdown.render import env
from opshub.projections import decisions_table

__all__ = [
    "DECISIONS_INDEX_FILENAME",
    "DecisionRow",
    "DecisionsRenderer",
    "render_decisions_markdown",
]


DECISIONS_INDEX_FILENAME = "index.md"
"""Filename of the decisions index in the rendered mapping."""


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """Presentation-side view of one ``decisions`` row."""

    id: str
    text: str
    context: str | None
    actor: str
    recorded_at: datetime


def render_decisions_markdown(decisions: list[DecisionRow]) -> dict[str, str]:
    """Render ``decisions`` into a ``{filename: content}`` mapping.

    The mapping always contains :data:`DECISIONS_INDEX_FILENAME` plus one
    ``<decision.id>.md`` per row. The index is sorted by
    ``(recorded_at DESC, id ASC)`` — the lexicographic id tie-break keeps
    output stable across back-to-back inserts.

    Rendering is deterministic: feeding the same list twice produces
    byte-identical strings — required by the workspace driver's
    idempotency contract.
    """
    environment = env()
    decision_template = environment.get_template("decision.md.j2")
    index_template = environment.get_template("decision_index.md.j2")

    index_decisions = sorted(
        decisions,
        key=lambda d: (-d.recorded_at.timestamp(), d.id),
    )

    result: dict[str, str] = {
        DECISIONS_INDEX_FILENAME: index_template.render(decisions=index_decisions),
    }
    for decision in decisions:
        result[f"{decision.id}.md"] = decision_template.render(decision=decision)
    return result


class DecisionsRenderer:
    """:class:`~opshub.markdown.workspace.WorkspaceRenderer` for ``decisions``.

    Owns the ``workspace_root / "generated" / "decisions" /`` subdirectory.
    """

    subdir: tuple[str, ...] = ("generated", "decisions")

    def read_and_render(self, engine: Engine) -> dict[str, str]:
        """Load every ``decisions`` row and render them to markdown."""
        rows = self._read_rows(engine)
        return render_decisions_markdown(rows)

    @staticmethod
    def _read_rows(engine: Engine) -> list[DecisionRow]:
        """Load every ``decisions`` row and adapt to the view-model."""
        stmt = select(decisions_table)
        with engine.connect() as conn:
            result = conn.execute(stmt).mappings().all()
        return [
            DecisionRow(
                id=row["id"],
                text=row["text"],
                context=row["context"],
                actor=row["actor"],
                recorded_at=_as_datetime(row["recorded_at"]),
            )
            for row in result
        ]


def _as_datetime(value: object) -> datetime:
    """Narrow a SQLAlchemy column value to :class:`datetime`."""
    if not isinstance(value, datetime):  # pragma: no cover - defensive
        raise TypeError(f"expected datetime from decisions projection, got {type(value).__name__}")
    return value
