"""Rendering helpers for ``opshub handoff list``.

Lives next to :mod:`opshub.cli.handoff` so the command callback stays
a thin typer wrapper. Three output formats are supported:

* ``table`` — aligned fixed-width columns for human eyeballing on a
  terminal.
* ``json`` — a JSON array of row dicts, suitable for piping into
  ``jq``.
* ``md`` — a GitHub-flavoured Markdown table, useful for pasting into
  PR descriptions / agent transcripts.

TODO(phase-2-step-3): Step 3 introduces ``cli/_render.py`` with shared
``render_table`` / ``render_json`` / ``render_md`` primitives and a
``Column`` descriptor. When step 3 merges, fold this module's three
renderers into a single ``columns`` definition + ``render_*(rows,
columns)`` dispatch (the same migration ``_task_list.py`` will do).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from opshub.core.errors import ValidationError

if TYPE_CHECKING:
    from opshub.services.handoff_service import HandoffRow

__all__ = ["render_open_handoffs"]

_ALLOWED_FORMATS = ("table", "json", "md")

# Column widths for the ``table`` format. ``topic`` is the only column
# that may be truncated; everything else is fixed-width by construction.
_ID_PREFIX_LEN = 8
_FROM_WIDTH = 16
_TO_WIDTH = 16
_TOPIC_WIDTH = 40
_OPENED_AT_WIDTH = 10  # "YYYY-MM-DD"
_ELLIPSIS = "..."


def render_open_handoffs(rows: list[HandoffRow], *, fmt: str) -> str:
    """Render open ``handoffs`` rows in ``fmt`` format.

    Parameters
    ----------
    rows:
        Pre-fetched value objects from
        :meth:`HandoffService.list_open`. The service already filters
        on ``state='open'`` and orders by ``opened_at DESC, id ASC``,
        so rendering does no additional querying or sorting.
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

    if fmt == "json":
        return _render_json(rows)
    if fmt == "md":
        return _render_md(rows)
    return _render_table(rows)


def _render_json(rows: list[HandoffRow]) -> str:
    """Serialise rows as a JSON array. Datetimes use ISO 8601 format."""
    return json.dumps([_jsonable_row(row) for row in rows], ensure_ascii=False)


def _jsonable_row(row: HandoffRow) -> dict[str, Any]:
    """Convert one :class:`HandoffRow` to a JSON-friendly dict."""
    return {
        "id": row.id,
        "from_actor": row.from_actor,
        "to_actor": row.to_actor,
        "topic": row.topic,
        "state": row.state,
        "opened_at": _iso(row.opened_at),
        "closed_at": _iso(row.closed_at),
        "note": row.note,
    }


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO 8601, or pass ``None`` through."""
    if value is None:
        return None
    return value.isoformat()


def _render_md(rows: list[HandoffRow]) -> str:
    """Render rows as a GitHub-flavoured Markdown table."""
    header = "| ID | From | To | Topic | Opened |"
    separator = "| --- | --- | --- | --- | --- |"
    if not rows:
        return "\n".join([header, separator])
    body_lines = [
        (
            f"| {_id_prefix(row.id)} "
            f"| {_escape_md(row.from_actor)} "
            f"| {_escape_md(row.to_actor)} "
            f"| {_escape_md(row.topic)} "
            f"| {_format_date(row.opened_at)} |"
        )
        for row in rows
    ]
    return "\n".join([header, separator, *body_lines])


def _render_table(rows: list[HandoffRow]) -> str:
    """Render rows as an aligned plain-text table."""
    header = (
        f"{'ID':<{_ID_PREFIX_LEN}}  "
        f"{'FROM':<{_FROM_WIDTH}}  "
        f"{'TO':<{_TO_WIDTH}}  "
        f"{'TOPIC':<{_TOPIC_WIDTH}}  "
        f"{'OPENED':<{_OPENED_AT_WIDTH}}"
    )
    if not rows:
        return header
    body_lines = [
        (
            f"{_id_prefix(row.id):<{_ID_PREFIX_LEN}}  "
            f"{_truncate(row.from_actor, _FROM_WIDTH):<{_FROM_WIDTH}}  "
            f"{_truncate(row.to_actor, _TO_WIDTH):<{_TO_WIDTH}}  "
            f"{_truncate(row.topic, _TOPIC_WIDTH):<{_TOPIC_WIDTH}}  "
            f"{_format_date(row.opened_at):<{_OPENED_AT_WIDTH}}"
        )
        for row in rows
    ]
    return "\n".join([header, *body_lines])


def _id_prefix(handoff_id: str) -> str:
    """Return the first ``_ID_PREFIX_LEN`` characters of a ULID."""
    return handoff_id[:_ID_PREFIX_LEN]


def _truncate(value: str, width: int) -> str:
    """Truncate ``value`` to ``width`` characters, appending ``...`` if cut."""
    if len(value) <= width:
        return value
    if width <= len(_ELLIPSIS):
        return value[:width]
    return value[: width - len(_ELLIPSIS)] + _ELLIPSIS


def _format_date(value: Any) -> str:
    """Render a datetime / date-ish value as ``YYYY-MM-DD``.

    SQLite's stdlib driver may return ``DateTime(timezone=True)``
    columns as naive datetimes whose components reflect UTC. We accept
    both flavours and fall back to ``str(value)`` for anything
    unexpected so a bad cell never crashes the render path.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)


def _escape_md(value: str) -> str:
    """Escape pipe characters so cell content does not break the table."""
    return value.replace("|", "\\|")
