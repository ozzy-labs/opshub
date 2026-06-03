"""Rendering helpers for ``opshub slack mentions list`` (Phase 18-B, ADR-0033).

Lives next to :mod:`opshub.cli.slack` so the command callback stays a
thin :mod:`typer` wrapper. Reads from the
:class:`opshub.projections.slack_demand_digest.slack_demand_digest_table`
projection that Phase 18-B materialised from
:class:`opshub.domain.events.SourceObserved` events.

The subcommand is **debug / operator-facing only** (ADR-0033
§Implementation plan 18-B). The first-class skill surface lands in
Phase 18-C as the MCP ``slack.demand.list`` tool — the CLI exists so
operators can inspect the projection during rollout and during
incidents (e.g. "did the latest sync land my DM?") without writing SQL
against the SQLite file.

Output shape
------------

* ``table`` — six columns: ``CHANNEL`` / ``TYPE`` / ``KIND`` /
  ``LAST_DEMAND`` / ``FROM`` / ``EXCERPT``. ``LAST_DEMAND`` renders
  as ``YYYY-MM-DD HH:MM:SS`` UTC so an operator can compare against
  Slack's own timestamps without timezone arithmetic.
* ``json`` — full row schema (every column plus ``last_source_id``),
  suitable for piping into ``jq``.
* ``md`` — GitHub-flavoured Markdown table; same six columns as the
  table view so a digest snippet pasted into a PR description renders
  legibly.

Filtering
---------

* ``--types`` accepts a comma-separated subset of
  :data:`opshub.projections.slack_demand_digest.CHANNEL_TYPES`
  (``im,mpim,private,public``); default = no filter.
* ``--demand-kind`` accepts a comma-separated subset of
  :data:`opshub.projections.slack_demand_digest.DEMAND_KINDS`
  (``mention,dm``); default = no filter.
* ``--limit`` caps the row count after sort; default 50 (matches
  ADR-0033 §Decision (c) MCP tool default).

Sort
----

Always ``last_demand_ts DESC`` so the most recent demand sits at the
top of the listing. Tiebreaker = ``channel_id ASC`` so the order is
deterministic across runs even when two channels share a
millisecond-precision ts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from opshub.cli._render import Column, dispatch, truncate
from opshub.core.errors import ValidationError
from opshub.projections.slack_demand_digest import (
    CHANNEL_TYPES,
    DEMAND_KINDS,
    slack_demand_digest_table,
)

__all__ = [
    "ALLOWED_FORMATS",
    "DEFAULT_LIMIT",
    "parse_demand_kinds",
    "parse_types",
    "render_mentions_list",
]


#: Output formats accepted by ``opshub slack mentions list``. Mirrors
#: the standard :func:`opshub.cli._render.dispatch` contract — the
#: ``--format`` validation is done up front in the typer callback so a
#: bad value exits with code 2 (usage error, see ``cli.app.main``).
ALLOWED_FORMATS: tuple[str, ...] = ("table", "json", "md")

#: Default row cap when ``--limit`` is omitted. Aligned with ADR-0033
#: §Decision (c) — the future MCP ``slack.demand.list`` tool ships
#: the same default so operator and skill see the same page size.
DEFAULT_LIMIT = 50

# --------------------------------------------------------------- column widths

_CHANNEL_WIDTH = 32  # channel id (11 char) + "#" + short name truncation
_TYPE_WIDTH = 7  # max len("private")
_KIND_WIDTH = 7  # max len("mention")
_LAST_DEMAND_WIDTH = 19  # "YYYY-MM-DD HH:MM:SS"
_FROM_WIDTH = 16
_EXCERPT_WIDTH = 60


def _format_channel_column(row: dict[str, Any]) -> str:
    """Render ``CHANNEL`` cell — id with the resolved name if known."""
    channel_id = str(row["channel_id"])
    channel_name = row.get("channel_name")
    if channel_name:
        return f"{channel_id} #{channel_name}"
    return channel_id


def _format_last_demand_column(row: dict[str, Any]) -> str:
    """Render ``LAST_DEMAND`` cell — ``YYYY-MM-DD HH:MM:SS`` UTC.

    ``last_demand_ts`` is a Slack-format Unix epoch float
    (``"1700000000.123456"``); the column converts to UTC and drops
    sub-second precision so the cell fits inside
    :data:`_LAST_DEMAND_WIDTH`.
    """
    ts = row["last_demand_ts"]
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return "-"


def _format_from_column(row: dict[str, Any]) -> str:
    """Render ``FROM`` cell — the Slack ``U...`` id if recorded.

    The Phase 18-B projection writes ``NULL`` for every row because
    :class:`SourceObserved` does not currently carry the message
    author id (only the resolved display name lands in ``title``).
    Renders as ``"-"`` until a future connector enhancement threads
    the user id through (tracked in ADR-0033 §Consequences §scope
    外).
    """
    value = row.get("last_demand_user_id")
    return str(value) if value else "-"


def _format_excerpt_column(row: dict[str, Any]) -> str:
    """Render ``EXCERPT`` cell — truncated body preview."""
    excerpt = row.get("last_demand_excerpt") or ""
    return truncate(str(excerpt), _EXCERPT_WIDTH)


# Single source of truth for the human-readable view (``table`` / ``md``).
_DISPLAY_COLUMNS: list[Column] = [
    Column(
        header="CHANNEL",
        accessor=_format_channel_column,
        width=_CHANNEL_WIDTH,
        json_key="channel",
    ),
    Column(
        header="TYPE",
        accessor=lambda row: str(row["channel_type"]),
        width=_TYPE_WIDTH,
        json_key="type",
    ),
    Column(
        header="KIND",
        accessor=lambda row: str(row["demand_kind"]),
        width=_KIND_WIDTH,
        json_key="kind",
    ),
    Column(
        header="LAST_DEMAND",
        accessor=_format_last_demand_column,
        width=_LAST_DEMAND_WIDTH,
        json_key="last_demand",
    ),
    Column(
        header="FROM",
        accessor=_format_from_column,
        width=_FROM_WIDTH,
        json_key="from",
    ),
    Column(
        header="EXCERPT",
        accessor=_format_excerpt_column,
        width=_EXCERPT_WIDTH,
        json_key="excerpt",
    ),
]


# JSON consumers want the full row (no width / truncation) plus a few
# columns the table view collapses (channel id vs. name, source id for
# join-back into the ``sources`` projection).
_JSON_COLUMNS: list[Column] = [
    Column(
        header="Channel ID",
        accessor=lambda row: row["channel_id"],
        json_key="channel_id",
    ),
    Column(
        header="Channel Type",
        accessor=lambda row: row["channel_type"],
        json_key="channel_type",
    ),
    Column(
        header="Channel Name",
        accessor=lambda row: row.get("channel_name"),
        json_key="channel_name",
    ),
    Column(
        header="Demand Kind",
        accessor=lambda row: row["demand_kind"],
        json_key="demand_kind",
    ),
    Column(
        header="Last Demand Ts",
        accessor=lambda row: row["last_demand_ts"],
        json_key="last_demand_ts",
    ),
    Column(
        header="Last Demand User ID",
        accessor=lambda row: row.get("last_demand_user_id"),
        json_key="last_demand_user_id",
    ),
    Column(
        header="Last Demand Excerpt",
        accessor=lambda row: row.get("last_demand_excerpt"),
        json_key="last_demand_excerpt",
    ),
    Column(
        header="Last Demand Permalink",
        accessor=lambda row: row.get("last_demand_permalink"),
        json_key="last_demand_permalink",
    ),
    Column(
        header="Last Source ID",
        accessor=lambda row: row.get("last_source_id"),
        json_key="last_source_id",
    ),
    Column(
        header="Updated At",
        accessor=lambda row: row["updated_at"],
        json_key="updated_at",
    ),
]


# --------------------------------------------------------------- parsing


def parse_types(raw: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated ``--types`` argument.

    Returns ``None`` for "no filter" (default), a non-empty tuple of
    validated :data:`CHANNEL_TYPES` values otherwise. Each entry is
    NFC-stripped and lower-cased before validation so a stray
    whitespace / capital letter does not surface as an opaque error.
    """
    if raw is None or not raw.strip():
        return None
    seen: set[str] = set()
    result: list[str] = []
    for token in raw.split(","):
        normalised = token.strip().lower()
        if not normalised:
            continue
        if normalised not in CHANNEL_TYPES:
            raise ValidationError(
                f"invalid --types value {normalised!r}; "
                f"expected a subset of {', '.join(CHANNEL_TYPES)}"
            )
        if normalised not in seen:
            seen.add(normalised)
            result.append(normalised)
    if not result:
        return None
    return tuple(result)


def parse_demand_kinds(raw: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated ``--demand-kind`` argument.

    Returns ``None`` for "no filter" (default), a non-empty tuple of
    validated :data:`DEMAND_KINDS` values otherwise.
    """
    if raw is None or not raw.strip():
        return None
    seen: set[str] = set()
    result: list[str] = []
    for token in raw.split(","):
        normalised = token.strip().lower()
        if not normalised:
            continue
        if normalised not in DEMAND_KINDS:
            raise ValidationError(
                f"invalid --demand-kind value {normalised!r}; "
                f"expected a subset of {', '.join(DEMAND_KINDS)}"
            )
        if normalised not in seen:
            seen.add(normalised)
            result.append(normalised)
    if not result:
        return None
    return tuple(result)


# --------------------------------------------------------------- render


def render_mentions_list(
    engine: Engine,
    *,
    fmt: str,
    types: tuple[str, ...] | None,
    demand_kinds: tuple[str, ...] | None,
    limit: int,
) -> str:
    """Render the digest projection in ``fmt`` format.

    Parameters
    ----------
    engine:
        SQLAlchemy engine bound to a database with the
        ``slack_demand_digest`` table (i.e. ``opshub init`` has been
        run and migration 0029 applied).
    fmt:
        One of ``"table"`` / ``"json"`` / ``"md"``.
    types:
        Optional ``channel_type`` filter, ``None`` = all.
    demand_kinds:
        Optional ``demand_kind`` filter, ``None`` = all.
    limit:
        Maximum row count after sort; must be ``>= 1``.

    Raises
    ------
    ValidationError
        If ``fmt`` is outside :data:`ALLOWED_FORMATS` or ``limit < 1``.
    """
    if fmt not in ALLOWED_FORMATS:
        raise ValidationError(
            f"invalid --format {fmt!r}; expected one of {', '.join(ALLOWED_FORMATS)}"
        )
    if limit < 1:
        raise ValidationError(
            f"invalid --limit {limit!r}; expected a positive integer"
        )

    rows = _fetch_rows(
        engine,
        types=types,
        demand_kinds=demand_kinds,
        limit=limit,
    )
    columns = _JSON_COLUMNS if fmt == "json" else _DISPLAY_COLUMNS
    return dispatch(fmt, columns, rows)


def _fetch_rows(
    engine: Engine,
    *,
    types: tuple[str, ...] | None,
    demand_kinds: tuple[str, ...] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Read the digest table, optionally filtered by type / kind.

    Sort order pins the contract: ``last_demand_ts DESC, channel_id
    ASC``. The ``ix_slack_demand_digest_last_demand_ts`` index covers
    the primary sort key; the ``channel_id`` tiebreaker keeps the row
    order deterministic across runs.
    """
    statement = select(slack_demand_digest_table).order_by(
        slack_demand_digest_table.c.last_demand_ts.desc(),
        slack_demand_digest_table.c.channel_id.asc(),
    )
    if types is not None:
        statement = statement.where(
            slack_demand_digest_table.c.channel_type.in_(types)
        )
    if demand_kinds is not None:
        statement = statement.where(
            slack_demand_digest_table.c.demand_kind.in_(demand_kinds)
        )
    statement = statement.limit(limit)

    with engine.connect() as conn:
        result = conn.execute(statement).mappings().all()
    return [dict(row) for row in result]
