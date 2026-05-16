"""Rendering helpers for ``opshub task list``.

Lives next to :mod:`opshub.cli.task` so the command callback stays a thin
typer wrapper. The actual table / json / md rendering logic is shared
with the other ``... list`` subcommands (``inbox list`` etc.) via
:mod:`opshub.cli._render` — this module owns only the row fetch and the
:class:`~opshub.cli._render.Column` descriptors specific to the ``tasks``
projection.

Rows are ordered by ``updated_at DESC, id ASC`` so the most recently touched
tasks appear at the top. ``id ASC`` is the deterministic tie-breaker for
events whose ``updated_at`` lands in the same millisecond (ULIDs are
monotonic per millisecond).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.cli._render import Column, dispatch, format_date, id_prefix, truncate
from opshub.core.errors import ValidationError
from opshub.projections.tasks import tasks_table

__all__ = ["render_task_list"]

_ALLOWED_FORMATS = ("table", "json", "md")
_ALLOWED_STATES = ("draft", "active", "completed")

# Column widths for the ``table`` format. ``title`` is the only column that
# may be truncated; everything else is fixed-width by construction.
_ID_PREFIX_LEN = 8
_TITLE_WIDTH = 40
_STATE_WIDTH = 9  # "completed"
_UPDATED_AT_WIDTH = 10  # "YYYY-MM-DD"


# Single source of truth for the ``task list`` view. The three render
# paths (table / json / md) all read off this list, so adding or
# reshaping a column is a one-line edit.
_COLUMNS: list[Column] = [
    Column(
        header="ID",
        accessor=lambda row: id_prefix(row["id"], _ID_PREFIX_LEN),
        width=_ID_PREFIX_LEN,
        json_key="id",
    ),
    Column(
        header="State",
        accessor=lambda row: row["state"],
        width=_STATE_WIDTH,
        json_key="state",
    ),
    Column(
        header="Title",
        accessor=lambda row: truncate(str(row["title"]), _TITLE_WIDTH),
        width=_TITLE_WIDTH,
        json_key="title",
    ),
    Column(
        header="Updated",
        accessor=lambda row: format_date(row["updated_at"]),
        width=_UPDATED_AT_WIDTH,
        json_key="updated_at",
    ),
]


# Columns used for the ``json`` format. JSON consumers care about the
# *full* row (id, title, body, state, result_note, timestamps) rather
# than the human-trimmed view above, so we keep a separate descriptor
# list. Datetimes are serialised by :func:`render_json` via
# ``isoformat()``; everything else is passed through as-is.
_JSON_COLUMNS: list[Column] = [
    Column(header="ID", accessor=lambda row: row["id"], json_key="id"),
    Column(header="Title", accessor=lambda row: row["title"], json_key="title"),
    Column(header="Body", accessor=lambda row: row["body"], json_key="body"),
    Column(header="State", accessor=lambda row: row["state"], json_key="state"),
    Column(
        header="Result Note",
        accessor=lambda row: row["result_note"],
        json_key="result_note",
    ),
    Column(
        header="Created At",
        accessor=lambda row: row["created_at"],
        json_key="created_at",
    ),
    Column(
        header="Updated At",
        accessor=lambda row: row["updated_at"],
        json_key="updated_at",
    ),
]


def render_task_list(
    engine: Engine,
    *,
    fmt: str,
    state_filter: str | None,
) -> str:
    """Render the ``tasks`` projection in ``fmt`` format.

    Parameters
    ----------
    engine:
        SQLAlchemy engine bound to a database that has the ``tasks`` table
        (i.e. ``opshub init`` has been run).
    fmt:
        One of ``"table"``, ``"json"``, ``"md"``.
    state_filter:
        Optional ``"draft" | "active" | "completed"`` filter applied
        server-side. ``None`` returns every row.

    Raises
    ------
    ValidationError
        If ``fmt`` or ``state_filter`` is outside the allowed set.
    """
    if fmt not in _ALLOWED_FORMATS:
        raise ValidationError(
            f"invalid --format {fmt!r}; expected one of {', '.join(_ALLOWED_FORMATS)}"
        )
    if state_filter is not None and state_filter not in _ALLOWED_STATES:
        raise ValidationError(
            f"invalid --state {state_filter!r}; expected one of {', '.join(_ALLOWED_STATES)}"
        )

    rows = _fetch_rows(engine, state_filter=state_filter)
    # JSON gets the full row schema; table / md get the trimmed eyeballing view.
    columns = _JSON_COLUMNS if fmt == "json" else _COLUMNS
    return dispatch(fmt, columns, rows)


def _fetch_rows(engine: Engine, *, state_filter: str | None) -> list[dict[str, Any]]:
    """Read the ``tasks`` table, optionally filtered by state.

    Sort order matches the documented contract: ``updated_at DESC, id ASC``.
    """
    statement = select(tasks_table).order_by(
        tasks_table.c.updated_at.desc(),
        tasks_table.c.id.asc(),
    )
    if state_filter is not None:
        statement = statement.where(tasks_table.c.state == state_filter)

    with engine.connect() as conn:
        result = conn.execute(statement).mappings().all()
    return [dict(row) for row in result]
