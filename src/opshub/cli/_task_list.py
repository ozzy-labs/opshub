"""Rendering helpers for ``opshub task list``.

Lives next to :mod:`opshub.cli.task` so the command callback stays a thin
typer wrapper. Three output formats are supported:

* ``table`` — aligned fixed-width columns for human eyeballing on a terminal.
* ``json`` — a JSON array of row dicts, suitable for piping into ``jq``.
* ``md`` — a GitHub-flavoured Markdown table, useful for pasting into PR
  descriptions / agent transcripts.

Rows are ordered by ``updated_at DESC, id ASC`` so the most recently touched
tasks appear at the top. ``id ASC`` is the deterministic tie-breaker for
events whose ``updated_at`` lands in the same millisecond (ULIDs are
monotonic per millisecond).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

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
_ELLIPSIS = "..."


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

    if fmt == "json":
        return _render_json(rows)
    if fmt == "md":
        return _render_md(rows)
    return _render_table(rows)


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


def _render_json(rows: list[dict[str, Any]]) -> str:
    """Serialise rows as a JSON array. Datetimes use ISO 8601 format."""
    return json.dumps([_jsonable_row(row) for row in rows], ensure_ascii=False)


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert datetime columns to ISO strings; pass everything else through."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _render_md(rows: list[dict[str, Any]]) -> str:
    """Render rows as a GitHub-flavoured Markdown table."""
    header = "| ID | State | Title | Updated |"
    separator = "| --- | --- | --- | --- |"
    if not rows:
        return "\n".join([header, separator])
    body_lines = [
        "| {id} | {state} | {title} | {updated} |".format(
            id=_id_prefix(row["id"]),
            state=row["state"],
            title=_escape_md(str(row["title"])),
            updated=_format_date(row["updated_at"]),
        )
        for row in rows
    ]
    return "\n".join([header, separator, *body_lines])


def _render_table(rows: list[dict[str, Any]]) -> str:
    """Render rows as an aligned plain-text table."""
    header = (
        f"{'ID':<{_ID_PREFIX_LEN}}  "
        f"{'STATE':<{_STATE_WIDTH}}  "
        f"{'TITLE':<{_TITLE_WIDTH}}  "
        f"{'UPDATED':<{_UPDATED_AT_WIDTH}}"
    )
    if not rows:
        return header
    body_lines = [
        (
            f"{_id_prefix(row['id']):<{_ID_PREFIX_LEN}}  "
            f"{row['state']!s:<{_STATE_WIDTH}}  "
            f"{_truncate(str(row['title']), _TITLE_WIDTH):<{_TITLE_WIDTH}}  "
            f"{_format_date(row['updated_at']):<{_UPDATED_AT_WIDTH}}"
        )
        for row in rows
    ]
    return "\n".join([header, *body_lines])


def _id_prefix(task_id: str) -> str:
    """Return the first ``_ID_PREFIX_LEN`` characters of a ULID.

    Eight characters of a Crockford-base32 ULID are unique enough for
    eyeballing within a single user's task list (and a `task list --format
    json` is always available when the full ULID matters).
    """
    return task_id[:_ID_PREFIX_LEN]


def _truncate(value: str, width: int) -> str:
    """Truncate ``value`` to ``width`` characters, appending ``...`` if cut."""
    if len(value) <= width:
        return value
    if width <= len(_ELLIPSIS):
        return value[:width]
    return value[: width - len(_ELLIPSIS)] + _ELLIPSIS


def _format_date(value: Any) -> str:
    """Render a datetime / date-ish value as ``YYYY-MM-DD``.

    SQLite's stdlib driver may return ``DateTime(timezone=True)`` columns as
    naive datetimes whose components reflect UTC. We accept both flavours
    and fall back to ``str(value)`` for anything unexpected so a bad cell
    never crashes the render path.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)


def _escape_md(value: str) -> str:
    """Escape pipe characters so cell content does not break the table.

    Pipes are the column delimiter in Markdown tables; replacing them with
    an escaped form (``\\|``) keeps an arbitrary task title from corrupting
    the rendered output.
    """
    return value.replace("|", "\\|")
