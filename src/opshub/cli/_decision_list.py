"""Rendering helpers for ``opshub decision list``.

Lives next to :mod:`opshub.cli.decision` so the command callback stays a
thin typer wrapper. Three output formats are supported:

* ``table`` — aligned fixed-width columns for human eyeballing on a
  terminal.
* ``json`` — a JSON array of row dicts, suitable for piping into ``jq``.
* ``md`` — a GitHub-flavoured Markdown table, useful for pasting into PR
  descriptions / agent transcripts.

Rows are ordered by ``recorded_at DESC, id ASC`` so the most recently
recorded decisions appear at the top. ``id ASC`` is the deterministic
tie-breaker for events whose ``recorded_at`` lands in the same
millisecond (ULIDs are monotonic per millisecond).

This module is a stop-gap until Phase 2 step 3 lands the shared
``cli/_render.py`` helper. The shape mirrors :mod:`opshub.cli._task_list`
so the eventual refactor is a mechanical lift onto the shared module.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.core.errors import ValidationError
from opshub.projections.decisions import decisions_table

__all__ = ["render_decision_list"]

_ALLOWED_FORMATS = ("table", "json", "md")

# Column widths for the ``table`` format. ``text`` is the only column
# that may be truncated; everything else is fixed-width by construction.
_ID_PREFIX_LEN = 8
_TEXT_WIDTH = 60
_RECORDED_AT_WIDTH = 10  # "YYYY-MM-DD"
_ELLIPSIS = "..."


def render_decision_list(engine: Engine, *, fmt: str) -> str:
    """Render the ``decisions`` projection in ``fmt`` format.

    Parameters
    ----------
    engine:
        SQLAlchemy engine bound to a database that has the ``decisions``
        table (i.e. ``opshub init`` has been run).
    fmt:
        One of ``"table"``, ``"json"``, ``"md"``.

    Raises
    ------
    ValidationError
        If ``fmt`` is outside the allowed set.
    """
    if fmt not in _ALLOWED_FORMATS:
        raise ValidationError(
            f"invalid --format {fmt!r}; expected one of {', '.join(_ALLOWED_FORMATS)}"
        )

    rows = _fetch_rows(engine)

    if fmt == "json":
        return _render_json(rows)
    if fmt == "md":
        return _render_md(rows)
    return _render_table(rows)


def _fetch_rows(engine: Engine) -> list[dict[str, Any]]:
    """Read the ``decisions`` table sorted by ``recorded_at DESC, id ASC``."""
    statement = select(decisions_table).order_by(
        decisions_table.c.recorded_at.desc(),
        decisions_table.c.id.asc(),
    )
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
    header = "| ID | Text | Recorded |"
    separator = "| --- | --- | --- |"
    if not rows:
        return "\n".join([header, separator])
    body_lines = [
        "| {id} | {text} | {recorded} |".format(
            id=_id_prefix(row["id"]),
            text=_escape_md(str(row["text"])),
            recorded=_format_date(row["recorded_at"]),
        )
        for row in rows
    ]
    return "\n".join([header, separator, *body_lines])


def _render_table(rows: list[dict[str, Any]]) -> str:
    """Render rows as an aligned plain-text table."""
    header = (
        f"{'ID':<{_ID_PREFIX_LEN}}  {'TEXT':<{_TEXT_WIDTH}}  {'RECORDED':<{_RECORDED_AT_WIDTH}}"
    )
    if not rows:
        return header
    body_lines = [
        (
            f"{_id_prefix(row['id']):<{_ID_PREFIX_LEN}}  "
            f"{_truncate(str(row['text']), _TEXT_WIDTH):<{_TEXT_WIDTH}}  "
            f"{_format_date(row['recorded_at']):<{_RECORDED_AT_WIDTH}}"
        )
        for row in rows
    ]
    return "\n".join([header, *body_lines])


def _id_prefix(decision_id: str) -> str:
    """Return the first ``_ID_PREFIX_LEN`` characters of a ULID."""
    return decision_id[:_ID_PREFIX_LEN]


def _truncate(value: str, width: int) -> str:
    """Truncate ``value`` to ``width`` characters, appending ``...`` if cut."""
    if len(value) <= width:
        return value
    if width <= len(_ELLIPSIS):
        return value[:width]
    return value[: width - len(_ELLIPSIS)] + _ELLIPSIS


def _format_date(value: Any) -> str:
    """Render a datetime / date-ish value as ``YYYY-MM-DD``."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)


def _escape_md(value: str) -> str:
    """Escape pipe characters so cell content does not break the table."""
    return value.replace("|", "\\|")
