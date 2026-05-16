"""Shared CLI list-renderers.

Phase 2 step 3 — multiple subcommands (``task list``, ``inbox list``,
upcoming ``decision list`` / ``workspace`` listings) all materialise a
small set of rows and render them in one of three formats:

* ``table`` — aligned fixed-width columns for terminal eyeballing.
* ``json`` — a JSON array of row dicts, suitable for piping into ``jq``.
* ``md`` — a GitHub-flavoured Markdown table for PR descriptions and
  agent transcripts.

Rather than duplicating that pattern across every subcommand (Phase 1's
``cli/_task_list.py`` was the first implementation), this module exposes
a tiny :class:`Column` descriptor + three pure rendering functions. A
subcommand declares its columns once and calls :func:`dispatch` with the
requested format and the rows; the rendering is uniform across the CLI.

The module is intentionally dependency-light: only stdlib + ``typing``.
Each subcommand still owns its own row-fetching code (SQLAlchemy queries
talk to the projection tables), but the rendering layer no longer
re-implements column alignment / JSON serialisation / Markdown escaping
per command.

ADR-0001 cold-start discipline:

The CLI subcommand modules (``cli/task.py``, ``cli/inbox.py`` etc.) keep
their module-level imports tiny so ``opshub --help`` stays under the
300ms budget. ``_render`` lives behind the ``_`` prefix and is only
loaded via the lazy-import inside a command callback — so the helper
modules it pulls in (``json``, ``datetime``) never enter the cold-start
path. The companion test in ``tests/integration/test_cli_imports.py``
only checks public CLI modules; private helpers like this one are free
to ``import json`` at the top.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

__all__ = [
    "Column",
    "dispatch",
    "format_date",
    "id_prefix",
    "render_json",
    "render_md",
    "render_table",
    "truncate",
]


_ALLOWED_FORMATS = ("table", "json", "md")
_DEFAULT_ID_PREFIX_LEN = 8
_ELLIPSIS = "..."


@dataclass(frozen=True)
class Column:
    """Describes one rendered column.

    Attributes
    ----------
    header:
        Display label. Used as the table header, the Markdown header, and
        — lower-cased and underscored — as the JSON key.
    accessor:
        Callable taking the row (a mapping or arbitrary object) and
        returning the raw value for the cell. Letting the caller supply
        the access function avoids a hardcoded ``row[key]`` shape and
        lets the renderer accept dicts, dataclasses, or SQLAlchemy
        ``RowMapping`` instances interchangeably.
    width:
        Fixed display width in the ``table`` format. ``None`` means
        "auto" — the renderer falls back to ``len(header)``.
    md_align:
        Markdown column alignment hint (``"left"`` / ``"right"`` /
        ``"center"``) — controls the separator row formatting.
    json_key:
        Optional override for the JSON key. Defaults to a derived form
        of ``header`` (lower-cased, spaces → underscores).
    """

    header: str
    accessor: Callable[[Any], Any]
    width: int | None = None
    md_align: str = "left"
    json_key: str | None = None

    @property
    def effective_json_key(self) -> str:
        """Return the JSON key the column will emit.

        Defaults to ``header.lower().replace(" ", "_")`` so a column
        header of ``"Updated At"`` becomes ``"updated_at"`` — matching
        the existing ``opshub task list --format json`` shape.
        """
        if self.json_key is not None:
            return self.json_key
        return self.header.lower().replace(" ", "_")

    @property
    def effective_width(self) -> int:
        """Return the column width to use in the ``table`` format.

        ``None`` widths fall back to the header length so the table
        always has *something* to align against. Callers can still
        explicitly set a width to truncate long content.
        """
        return self.width if self.width is not None else len(self.header)


def dispatch(fmt: str, columns: Sequence[Column], rows: Sequence[Any]) -> str:
    """Render ``rows`` in ``fmt`` using ``columns``.

    Raises
    ------
    ValueError
        If ``fmt`` is not one of ``"table"``, ``"json"``, ``"md"``.
        Callers (subcommand modules) catch this and re-raise as
        :class:`opshub.core.errors.ValidationError` so the CLI exit-code
        mapping in ``cli/app.main()`` takes effect.
    """
    if fmt not in _ALLOWED_FORMATS:
        raise ValueError(f"invalid format {fmt!r}; expected one of {', '.join(_ALLOWED_FORMATS)}")
    if fmt == "json":
        return render_json(rows, columns)
    if fmt == "md":
        return render_md(rows, columns)
    return render_table(rows, columns)


def render_table(rows: Sequence[Any], columns: Sequence[Column]) -> str:
    """Render rows as an aligned fixed-width plain-text table.

    The header is always rendered (even when ``rows`` is empty) so a
    user piping into ``head`` sees the column names; this matches the
    existing ``task list`` behaviour.
    """
    header_cells = [f"{col.header.upper():<{col.effective_width}}" for col in columns]
    header_line = "  ".join(header_cells)
    if not rows:
        return header_line

    body_lines: list[str] = []
    for row in rows:
        cells: list[str] = []
        for col in columns:
            raw = col.accessor(row)
            text = _stringify(raw)
            if col.width is not None:
                text = truncate(text, col.width)
            cells.append(f"{text:<{col.effective_width}}")
        body_lines.append("  ".join(cells))
    return "\n".join([header_line, *body_lines])


def render_json(rows: Sequence[Any], columns: Sequence[Column]) -> str:
    """Render rows as a JSON array of objects keyed by column JSON-key.

    Datetime / date values are serialised via :func:`datetime.isoformat`
    so the JSON survives a ``jq`` pipe without losing tzinfo (when
    present). Everything else is passed through to :func:`json.dumps`
    which raises for un-serialisable types — that is intentional;
    callers should pick accessors that return JSON-native values.
    """
    payload: list[dict[str, Any]] = []
    for row in rows:
        payload.append({col.effective_json_key: _jsonable(col.accessor(row)) for col in columns})
    return json.dumps(payload, ensure_ascii=False)


def render_md(rows: Sequence[Any], columns: Sequence[Column]) -> str:
    """Render rows as a GitHub-flavoured Markdown table.

    The separator row encodes ``md_align`` per column:
    ``:---`` (left), ``---:`` (right), ``:---:`` (center). The base
    ``---`` form (no colon) renders as left-aligned in GitHub, so
    callers that don't care about alignment can leave ``md_align`` at
    its default and get the same look as the existing ``task list``
    renderer.
    """
    header_line = "| " + " | ".join(col.header for col in columns) + " |"
    separator_line = "| " + " | ".join(_md_separator(col) for col in columns) + " |"
    if not rows:
        return "\n".join([header_line, separator_line])

    body_lines: list[str] = []
    for row in rows:
        cells = [_escape_md(_stringify(col.accessor(row))) for col in columns]
        body_lines.append("| " + " | ".join(cells) + " |")
    return "\n".join([header_line, separator_line, *body_lines])


# ---- public helpers -------------------------------------------------------


def id_prefix(value: str, length: int = _DEFAULT_ID_PREFIX_LEN) -> str:
    """Return the first ``length`` characters of a ULID-shaped string.

    Most CLI list views render an 8-character ULID prefix — long enough
    to disambiguate within a user's working set, short enough to scan
    quickly. Exported so callers can build :class:`Column` accessors
    without re-implementing the trim.
    """
    return value[:length]


def truncate(value: str, width: int) -> str:
    """Truncate ``value`` to ``width`` characters, suffixing ``...`` if cut.

    When ``width`` is smaller than the ellipsis itself, we fall back to
    a hard slice — ``...``-ing a 2-char column would look worse than
    just clipping.
    """
    if len(value) <= width:
        return value
    if width <= len(_ELLIPSIS):
        return value[:width]
    return value[: width - len(_ELLIPSIS)] + _ELLIPSIS


def format_date(value: Any) -> str:
    """Render a datetime / date / other value as ``YYYY-MM-DD``.

    SQLite's stdlib driver may surface ``DateTime(timezone=True)`` columns
    as naive datetimes whose components reflect UTC; both naive and
    aware datetimes are accepted. Anything else (string, ``None``, ...)
    falls through to ``str(value)`` so a single bad cell never crashes
    the render path.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# ---- private helpers ------------------------------------------------------


def _stringify(value: Any) -> str:
    """Coerce a cell value to a string for table / md rendering.

    Datetimes use the project-wide ``YYYY-MM-DD`` short form (full
    ISO strings are kept for the JSON path). ``None`` collapses to an
    empty string so an absent value doesn't bloat the column width.
    """
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return format_date(value)
    return str(value)


def _jsonable(value: Any) -> Any:
    """Return a JSON-serialisable representation of ``value``.

    Datetimes / dates are emitted as ISO strings (preserving precision
    and tzinfo when the source had it). Other types pass through —
    :func:`json.dumps` will raise on un-serialisable types, which is
    the right failure mode (the column accessor should be fixed, not
    the renderer).
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _md_separator(col: Column) -> str:
    """Build the Markdown separator cell honouring ``md_align``."""
    if col.md_align == "right":
        return "---:"
    if col.md_align == "center":
        return ":---:"
    return "---"


def _escape_md(value: str) -> str:
    """Escape pipe characters so cell content doesn't break the table.

    Pipes are the column delimiter in Markdown tables; replacing them
    with an escaped form (``\\|``) keeps arbitrary user input from
    corrupting the rendered output.
    """
    return value.replace("|", "\\|")
