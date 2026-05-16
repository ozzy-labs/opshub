"""Rendering helpers for ``opshub inbox list``.

Mirrors the shape of :mod:`opshub.cli._task_list`: this module owns the
SQLAlchemy query against :data:`opshub.projections.inbox.inbox_items_table`
plus the :class:`~opshub.cli._render.Column` descriptors for the
inbox-specific view, and delegates all of the format-specific rendering
to :func:`opshub.cli._render.dispatch`.

Rows are ordered by ``created_at DESC, id ASC`` so the most recently
captured items appear at the top; ``id ASC`` is the deterministic
tie-breaker for events whose ``created_at`` lands in the same
millisecond.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.cli._render import Column, dispatch, format_date, id_prefix, truncate
from opshub.core.errors import ValidationError
from opshub.projections.inbox import inbox_items_table

__all__ = ["render_inbox_list"]

_ALLOWED_FORMATS = ("table", "json", "md")
_ALLOWED_STATES = (
    "pending",
    "triaged_to_task",
    "triaged_to_decision",
    "discarded",
)

# Column widths for the ``table`` format. ``summary`` is the only
# column that may be truncated; the rest are fixed-width by
# construction.
_ID_PREFIX_LEN = 8
_STATE_WIDTH = 20  # widest legal value is "triaged_to_decision" (19 chars)
_SUMMARY_WIDTH = 40
_CREATED_AT_WIDTH = 10  # "YYYY-MM-DD"


# Single source of truth for the ``inbox list`` table / md view.
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
        header="Summary",
        accessor=lambda row: truncate(str(row["summary"]), _SUMMARY_WIDTH),
        width=_SUMMARY_WIDTH,
        json_key="summary",
    ),
    Column(
        header="Created",
        accessor=lambda row: format_date(row["created_at"]),
        width=_CREATED_AT_WIDTH,
        json_key="created_at",
    ),
]


# JSON format gets the full row schema — consumers piping into ``jq``
# expect every column on the projection table, not the human-trimmed
# view used by ``table`` / ``md``.
_JSON_COLUMNS: list[Column] = [
    Column(header="ID", accessor=lambda row: row["id"], json_key="id"),
    Column(header="Summary", accessor=lambda row: row["summary"], json_key="summary"),
    Column(
        header="Source Ref",
        accessor=lambda row: row["source_ref"],
        json_key="source_ref",
    ),
    Column(header="State", accessor=lambda row: row["state"], json_key="state"),
    Column(
        header="Disposition",
        accessor=lambda row: row["disposition"],
        json_key="disposition",
    ),
    Column(
        header="Target ID",
        accessor=lambda row: row["target_id"],
        json_key="target_id",
    ),
    Column(header="Reason", accessor=lambda row: row["reason"], json_key="reason"),
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


def render_inbox_list(
    engine: Engine,
    *,
    fmt: str,
    state_filter: str | None,
) -> str:
    """Render the ``inbox_items`` projection in ``fmt`` format.

    Parameters
    ----------
    engine:
        SQLAlchemy engine bound to a database that has the
        ``inbox_items`` table (i.e. ``opshub init`` has been run).
    fmt:
        One of ``"table"``, ``"json"``, ``"md"``.
    state_filter:
        Optional filter (``"pending" | "triaged_to_task" |
        "triaged_to_decision" | "discarded"``) applied server-side.
        ``None`` returns every row.

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
    columns = _JSON_COLUMNS if fmt == "json" else _COLUMNS
    return dispatch(fmt, columns, rows)


def _fetch_rows(engine: Engine, *, state_filter: str | None) -> list[dict[str, Any]]:
    """Read the ``inbox_items`` table, optionally filtered by state.

    Sort order matches the documented contract: ``created_at DESC, id
    ASC``. The ``id`` tie-breaker is deterministic for items captured
    in the same millisecond because ULIDs are monotonic per ms.
    """
    statement = select(inbox_items_table).order_by(
        inbox_items_table.c.created_at.desc(),
        inbox_items_table.c.id.asc(),
    )
    if state_filter is not None:
        statement = statement.where(inbox_items_table.c.state == state_filter)

    with engine.connect() as conn:
        result = conn.execute(statement).mappings().all()
    return [dict(row) for row in result]
