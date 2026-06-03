"""Formatters + driver for ``opshub slack conversations`` (#366).

Replaces the original ``_slack_channels`` helper from #341. The wrapper
in :mod:`opshub.cli.slack` calls
:func:`opshub.connectors.slack.conversations.list_conversations` —
which iterates ``users.conversations`` (default) or
``conversations.list`` (``--all``) — and renders the result in one of
three formats: ``table`` (default), ``toml``, or ``json``.

The helper lives behind a ``_`` prefix so the static cold-start guard
(``tests/integration/test_cli_imports``) does not require its
module-level imports to stay inside the whitelist (the parametrised
test only walks public ``cli/*.py`` modules). The public
:mod:`opshub.cli.connector` module still defers
``_slack_conversations`` import inside the command callback to preserve
the ADR-0001 cold-start budget for operators who never run the
``connector slack conversations`` subcommand.

Output formats
--------------

* ``table`` (default): five fixed-width columns ``ID``, ``TYPE``,
  ``NAME / PARTICIPANTS``, ``ARCHIVED``, ``PURPOSE``. The ``NAME /
  PARTICIPANTS`` column shows the channel name for public/private
  channels, the peer name for DMs, and a comma-joined participant
  list (capped at :data:`_MPIM_PARTICIPANT_DISPLAY_LIMIT` with a
  ``+N`` suffix) for MPIMs.
* ``toml``: emits a ``channels = [...]`` snippet ready to paste into
  ``opshub.toml`` under ``[connectors.slack]``. Each id is annotated
  with a comment carrying the conversation type and a human-readable
  label.
* ``json``: a JSON array of objects matching
  ``SlackConversation.__dataclass_fields__`` 1:1 (id / type / name /
  display_name / is_private / is_archived / purpose / participants).

Progress reporting
------------------

When run on a TTY (or with explicit ``--progress``), the helper
wraps the iterator in :func:`opshub.cli._progress.indeterminate` so
the operator sees a spinner + page count for slow workspaces. Driven
by the same auto-detection / env-var / flag precedence as
``connector sync`` (ADR-0026).

Token safety
------------

The Slack OAuth token never appears in any output here — the helper
operates on :class:`SlackConversation` rows that the upstream iterator
already stripped down to documented public fields. Error paths bubble
up :class:`opshub.core.errors.ConfigError` /
:class:`opshub.core.errors.ConnectorFailedError` whose messages are
sanitised by the upstream iterator + auth resolver.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import typer

from opshub.cli import _progress
from opshub.connectors.slack.conversations import (
    CONVERSATION_TYPES,
    ConversationType,
)
from opshub.core.time import now_utc

if TYPE_CHECKING:
    from collections.abc import Iterable

    from opshub.connectors.slack.conversations import SlackConversation


__all__ = [
    "DEFAULT_TYPES_CSV",
    "FORMAT_CHOICES",
    "OutputFormat",
    "parse_since",
    "parse_types",
    "render_conversations",
    "run_conversations_command",
]


#: Output format literal — kept in lock-step with the Typer
#: ``--format`` choice list :data:`FORMAT_CHOICES`.
OutputFormat = Literal["table", "toml", "json"]

#: Valid ``--format`` values surfaced to Typer.
FORMAT_CHOICES: tuple[OutputFormat, ...] = ("table", "toml", "json")

#: Default value for the Typer ``--types`` option. Spells out the full
#: accept-list (rather than rebuilding from :data:`CONVERSATION_TYPES`)
#: so the ``--help`` output is self-documenting and a future addition
#: to the type set forces a deliberate edit here.
DEFAULT_TYPES_CSV: str = "public,private,im,mpim"

#: Truncation cap for the ``PURPOSE`` column in the table output.
_PURPOSE_TRUNCATE_LEN = 40

#: Minimum column width for the ``NAME / PARTICIPANTS`` column. Slack
#: channel names cap at 80; participant lists can run long. We set the
#: floor at 24 so DM peer names and short channel names still align.
_NAME_MIN_WIDTH = 24

#: Minimum column width for the ``ID`` column. Slack ids are 9-11 chars
#: (``"C..."`` / ``"G..."`` / ``"D..."``); the floor at 11 keeps the
#: header aligned with the value column on workspaces with short ids.
_ID_MIN_WIDTH = 11

#: Minimum column width for the ``TYPE`` column. The widest token is
#: ``"private"`` (7 chars); we use 8 so headers and rows align.
_TYPE_WIDTH = 8

#: Maximum number of participant names to render in the MPIM ``NAME /
#: PARTICIPANTS`` column before falling back to ``+N`` notation. Three
#: keeps the column under ~60 chars on a typical 4-name MPIM
#: (``alice, bob, carol +2``) while still showing enough context for
#: the operator to identify the group.
_MPIM_PARTICIPANT_DISPLAY_LIMIT = 3

#: Fixed bucket order for the type-grouped sort. The CLI surface
#: documented in #366 pins this enumeration; the operator-visible
#: ordering follows ``public → private → mpim → im`` (channels first,
#: then group DMs, then 1:1 DMs) so a TOML paste lands public-channel
#: ids at the top where most opshub configs scope their first sync.
_TYPE_BUCKET_ORDER: tuple[ConversationType, ...] = ("public", "private", "mpim", "im")

#: Width for the ``LAST_ACTIVITY`` table column (``YYYY-MM-DD`` is
#: 10 chars; we pad to 13 so the column header and values align).
_LAST_ACTIVITY_WIDTH = 13

#: ``--since`` relative-form pattern. Accepts ``<N>d`` (days) and
#: ``<N>w`` (weeks). Months / years are intentionally unsupported —
#: their calendar semantics are ambiguous for an "is this channel
#: still active" filter and ``90d`` / ``365d`` cover the practical
#: range without extra surface to test.
_SINCE_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([dw])\s*$")


def parse_since(raw: str) -> datetime:
    """Parse a ``--since`` value into a tz-aware UTC :class:`datetime.datetime`.

    Accepts two forms (see :data:`_SINCE_RELATIVE_RE` for the relative
    grammar):

    * Relative: ``"7d"`` / ``"2w"`` → ``now_utc() - timedelta(...)``.
      ``"0d"`` is permitted and resolves to "now" (a degenerate but
      harmless filter the operator can construct via ``--since 0d``
      for a sanity check).
    * Absolute: any ISO 8601 string :func:`datetime.fromisoformat`
      accepts, plus the convenience that a trailing ``Z`` (UTC zulu)
      is rewritten to ``+00:00`` so ``"2026-05-01T00:00:00Z"`` parses
      cleanly. tz-naive inputs are interpreted as UTC.

    Raises :class:`typer.BadParameter` for empty input, unknown forms,
    and malformed numerics — Typer surfaces the message verbatim with
    exit code 2 so operators can self-correct without re-reading
    ``--help``.
    """
    if not raw or not raw.strip():
        raise typer.BadParameter("--since must not be empty")

    text = raw.strip()
    relative = _SINCE_RELATIVE_RE.match(text)
    if relative is not None:
        amount = int(relative.group(1))
        unit = relative.group(2)
        # ``\d+`` is unbounded, so a typo like ``--since 99999999999d``
        # would propagate to :class:`timedelta` and raise
        # :class:`OverflowError` (escaping the documented
        # :class:`typer.BadParameter` contract that surfaces with
        # exit code 2). Translate the overflow into the documented
        # usage-error vocabulary so the operator sees one consistent
        # ``--help``-able message.
        try:
            delta = timedelta(days=amount) if unit == "d" else timedelta(weeks=amount)
        except OverflowError as exc:
            raise typer.BadParameter(
                f"--since {raw!r} is too far in the past; use an ISO date "
                "(e.g. '2026-05-01') for cutoffs beyond a few centuries."
            ) from exc
        return now_utc() - delta

    iso_text = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--since {raw!r} is not a recognised value: expected a relative "
            "duration like '7d' / '2w' or an ISO date like '2026-05-01'."
        ) from exc

    if parsed.tzinfo is None:
        # Naive inputs default to UTC so the operator can write
        # ``--since 2026-05-01`` without manually annotating timezone.
        # ADR-0027 keeps internal tz handling on UTC; matching that
        # default here means cross-tz operators see no surprise drift.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_types(raw: str) -> tuple[ConversationType, ...]:
    """Parse a ``--types`` value (``"public,im"``) into a typed tuple.

    Raises :class:`typer.BadParameter` for empty input, unknown tokens,
    or whitespace-only entries — Typer surfaces the message verbatim
    with exit code 2 so operators can self-correct without re-reading
    the help.
    """
    if not raw.strip():
        raise typer.BadParameter("--types must not be empty")

    parts: list[ConversationType] = []
    seen: set[ConversationType] = set()
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        if token not in CONVERSATION_TYPES:
            allowed = ", ".join(CONVERSATION_TYPES)
            raise typer.BadParameter(f"unknown --types value {token!r}; choose from {allowed}")
        narrowed: ConversationType = token
        if narrowed in seen:
            continue
        seen.add(narrowed)
        parts.append(narrowed)

    if not parts:
        raise typer.BadParameter("--types must contain at least one type")
    return tuple(parts)


def run_conversations_command(
    *,
    output_format: OutputFormat,
    filter_substring: str | None,
    limit: int | None,
    types: tuple[ConversationType, ...],
    include_archived: bool,
    all: bool,
    since: datetime | None = None,
) -> None:
    """Drive ``opshub slack conversations`` end-to-end.

    This is the seam :mod:`opshub.cli.slack` calls. The handler in
    ``slack.py`` is intentionally thin so the lazy-import
    bookkeeping stays in one place and this helper covers the
    operator-visible behaviour.

    Parameters
    ----------
    output_format:
        One of :data:`FORMAT_CHOICES`.
    filter_substring:
        Case-insensitive substring match against ``name`` /
        ``display_name``. ``None`` (or empty string) disables filtering.
    limit:
        Maximum number of conversations to yield. ``None`` means no
        cap.
    types:
        Tuple of conversation types to request (parsed from
        ``--types``).
    include_archived:
        When ``True``, archived channels are included.
    all:
        When ``True``, switch from ``users.conversations`` (joined-
        only) to ``conversations.list`` (workspace-wide).
    since:
        Optional tz-aware :class:`datetime.datetime` cutoff. When
        non-``None``, the listing iterator makes one extra
        ``conversations.history?limit=1`` call per row and yields only
        rows whose latest message ts is newer than ``since``. The
        table renderer adds a ``LAST_ACTIVITY`` column in that mode;
        the sort within each type-bucket flips from ``display_name``
        ascending to ``last_activity_ts`` descending. ``None`` keeps
        the no-extra-API-call path of #366.

    Raises
    ------
    opshub.core.errors.ConfigError
        Bubbled up from :class:`SlackAuth` (no token / wrong prefix)
        or from the SDK extras gate.
    opshub.core.errors.ConnectorFailedError
        Bubbled up from :func:`list_conversations` (invalid_auth /
        missing_scope / exhausted 429 retries).
    """
    # Lazy-import the Slack subpackage so ``opshub --help`` cold start
    # never pays for ``slack_sdk`` (ADR-0001).
    from opshub.connectors.slack.auth import SlackAuth
    from opshub.connectors.slack.conversations import list_conversations

    auth = SlackAuth()

    # Wrap the iterator in the indeterminate progress reporter so the
    # operator sees a spinner + page-tick on slow workspaces. ``reporter``
    # is a no-op when progress is disabled (non-TTY / ``--no-progress``),
    # which keeps captured-output tests stable. When ``--since`` is set
    # the per-row ``conversations.history`` call happens inside the same
    # iterator so the spinner ticks cover both the listing pages and
    # the activity probes — a single description ("listing + activity")
    # keeps the operator's mental model tidy without forcing a second
    # rich Progress context.
    warnings: list[str] = []
    description = (
        "listing conversations + activity" if since is not None else "listing conversations"
    )
    with _progress.indeterminate(description) as reporter:
        conversations = list_conversations(
            auth,
            types=types,
            include_archived=include_archived,
            filter_substring=filter_substring,
            limit=limit,
            all=all,
            since=since,
            warnings=warnings,
            reporter=reporter,
        )

        # Materialise inside the reporter context so the spinner stays
        # active through the entire pagination walk. The discovery use
        # case is operator-interactive (paste-and-edit), not streaming,
        # so materialising the list is fine — ``--filter`` / ``--limit``
        # scope the output before this point.
        rows = list(conversations)

    for warning in warnings:
        # The iterator accumulates one warning per affected
        # conversation type (e.g. ``mpim:history`` missing). Surface
        # them on stderr after the spinner closes so they do not get
        # interleaved with the live progress display.
        typer.echo(warning, err=True)

    sorted_rows = _sort_rows(rows, by_activity=since is not None)

    if not sorted_rows and output_format != "json":
        _emit_empty_hint(filter_substring=filter_substring, err=True)

    rendered = render_conversations(
        sorted_rows,
        output_format=output_format,
        show_activity=since is not None,
    )
    if rendered:
        typer.echo(rendered)


def render_conversations(
    rows: Iterable[SlackConversation],
    *,
    output_format: OutputFormat,
    show_activity: bool = False,
) -> str:
    """Format a stream of :class:`SlackConversation` rows for stdout.

    Pure function (no side effects, no I/O) so unit tests can pass a
    list of fixtures and assert exact bytes — the
    :func:`run_conversations_command` wrapper is what shells out to
    ``typer.echo`` and reads from the network.

    ``show_activity`` controls whether the table layout includes the
    ``LAST_ACTIVITY`` column (default: hidden). The JSON / TOML
    renderers always reflect ``last_activity_ts`` when populated; the
    JSON renderer drops the key for rows where it is ``None`` so a
    no-``--since`` invocation does not pollute the payload with
    meaningless nulls.
    """
    materialised = list(rows)
    if output_format == "table":
        return _render_table(materialised, show_activity=show_activity)
    if output_format == "toml":
        return _render_toml(materialised)
    if output_format == "json":
        return _render_json(materialised)
    raise ValueError(f"unknown output format: {output_format!r}")


def _sort_rows(
    rows: Iterable[SlackConversation],
    *,
    by_activity: bool,
) -> list[SlackConversation]:
    """Sort rows by the documented fixed type buckets and within-bucket key.

    Type buckets follow :data:`_TYPE_BUCKET_ORDER` (``public →
    private → mpim → im``); rows of an unrecognised type bucket sort
    last in their original order. Within each bucket:

    * ``by_activity=True`` → ``last_activity_ts`` descending; rows
      missing the ts (defensive, should not occur when ``--since`` is
      set) sort last.
    * ``by_activity=False`` → ``display_name`` case-insensitive
      ascending; ``id`` is used as a stable tiebreaker so two rows
      sharing a display name keep a deterministic order across runs.
    """
    bucket_index = {bucket: idx for idx, bucket in enumerate(_TYPE_BUCKET_ORDER)}
    fallback_bucket = len(_TYPE_BUCKET_ORDER)

    if by_activity:

        def _activity_key(row: SlackConversation) -> tuple[int, int, float, str]:
            bucket = bucket_index.get(row.type, fallback_bucket)
            ts = row.last_activity_ts
            # ``has_ts`` (0 = present, 1 = missing) lifts missing-ts
            # rows to the bottom of the bucket; ``-ts`` flips the
            # sort to descending (newest first) for present ts.
            has_ts = 0 if ts is not None else 1
            ts_key = -ts if ts is not None else 0.0
            return (bucket, has_ts, ts_key, row.id)

        return sorted(rows, key=_activity_key)

    def _name_key(row: SlackConversation) -> tuple[int, str, str]:
        bucket = bucket_index.get(row.type, fallback_bucket)
        return (bucket, row.display_name.lower(), row.id)

    return sorted(rows, key=_name_key)


# ----- private helpers ---------------------------------------------------


def _emit_empty_hint(*, filter_substring: str | None, err: bool) -> None:
    """Print the ``no conversations matched`` stderr hint for empty results."""
    if filter_substring:
        typer.echo(
            f"no conversations matched (filter: {filter_substring!r})",
            err=err,
        )
    else:
        typer.echo("no conversations matched", err=err)


def _render_table(rows: list[SlackConversation], *, show_activity: bool = False) -> str:
    """Render conversations as a fixed-width table.

    ``show_activity=True`` inserts a ``LAST_ACTIVITY`` column between
    ``ARCHIVED`` and ``PURPOSE``. The column renders the UTC date
    (``YYYY-MM-DD``) of the per-row ``last_activity_ts``; rows
    without a ts (defensive, should not occur when the caller invokes
    ``--since``) render ``-``.
    """
    name_values = [_format_name_column(row) for row in rows]

    id_width = max(_ID_MIN_WIDTH, max((len(row.id) for row in rows), default=0))
    name_width = max(_NAME_MIN_WIDTH, max((len(v) for v in name_values), default=0))
    archived_width = 8

    header_parts = [
        f"{'ID':<{id_width}}",
        f"{'TYPE':<{_TYPE_WIDTH}}",
        f"{'NAME / PARTICIPANTS':<{name_width}}",
        f"{'ARCHIVED':<{archived_width}}",
    ]
    if show_activity:
        header_parts.append(f"{'LAST_ACTIVITY':<{_LAST_ACTIVITY_WIDTH}}")
    header_parts.append("PURPOSE")
    header = "  ".join(header_parts)

    lines = [header]
    for row, name_value in zip(rows, name_values, strict=True):
        purpose = _truncate(row.purpose, _PURPOSE_TRUNCATE_LEN)
        archived = _yes_no(row.is_archived) if _supports_archive(row) else "-"
        cells = [
            f"{row.id:<{id_width}}",
            f"{row.type:<{_TYPE_WIDTH}}",
            f"{name_value:<{name_width}}",
            f"{archived:<{archived_width}}",
        ]
        if show_activity:
            cells.append(f"{_format_activity_date(row.last_activity_ts):<{_LAST_ACTIVITY_WIDTH}}")
        cells.append(purpose)
        lines.append("  ".join(cells))
    return "\n".join(lines)


def _format_activity_date(ts: float | None) -> str:
    """Render a Slack ts as ``YYYY-MM-DD`` (UTC) or ``-`` when missing."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _format_name_column(row: SlackConversation) -> str:
    """Build the ``NAME / PARTICIPANTS`` column value for one row.

    Public / private channels render ``name`` (with ``(private)`` flag
    appended for visual disambiguation). DMs render the peer's display
    name. MPIMs render up to :data:`_MPIM_PARTICIPANT_DISPLAY_LIMIT`
    participant names with a ``+N`` suffix for the remainder.
    """
    if row.type == "mpim":
        if not row.participants:
            return row.display_name or row.id
        head = list(row.participants[:_MPIM_PARTICIPANT_DISPLAY_LIMIT])
        remainder = len(row.participants) - len(head)
        joined = ", ".join(head)
        if remainder > 0:
            return f"{joined} +{remainder}"
        return joined
    if row.type == "im":
        return row.display_name or row.id
    # public / private channel
    if row.type == "private":
        return f"{row.name or row.id} (private)"
    return row.name or row.id


def _supports_archive(row: SlackConversation) -> bool:
    """Return True for row types that can be archived (channels only).

    DM / MPIM never archive in Slack; rendering ``no`` for them implies
    they could be — ``-`` is the clearer null marker.
    """
    return row.type in ("public", "private")


def _render_toml(rows: list[SlackConversation]) -> str:
    """Render conversations as a TOML ``channels = [...]`` snippet.

    Each id sits inside string quotes followed by a comment that names
    the conversation type and a human-readable label so reviewers can
    spot DMs / MPIMs / archived entries at a glance before pasting
    into ``opshub.toml``.
    """
    header = f"# Slack conversations ({len(rows)})"
    if not rows:
        return f"{header}\nchannels = []"
    lines = [header, "channels = ["]
    for row in rows:
        flags: list[str] = [row.type]
        if row.is_archived:
            flags.append("archived")
        if row.last_activity_ts is not None:
            # Operators reviewing a TOML paste want the activity
            # signal inline with the channel id; carrying it in the
            # comment keeps the actual config (``"C..."``) untouched
            # while still showing the reviewer "this channel last
            # spoke on 2026-05-30".
            flags.append(f"last {_format_activity_date(row.last_activity_ts)}")
        label = row.name or row.display_name or row.id
        comment = f"{label} ({', '.join(flags)})"
        lines.append(f'  "{row.id}",  # {comment}')
    lines.append("]")
    return "\n".join(lines)


def _render_json(rows: list[SlackConversation]) -> str:
    """Render conversations as a JSON array of dataclass dicts.

    Uses :func:`dataclasses.asdict` so the field order matches the
    dataclass definition. ``participants`` lands as a JSON array (the
    dataclass field type is ``tuple[str, ...]`` but :func:`asdict` and
    :func:`json.dumps` lower tuples to arrays naturally).

    ``last_activity_ts`` is omitted from rows where it is ``None`` so
    a no-``--since`` invocation does not pollute the payload with
    meaningless null fields — keeps the JSON contract of #366 intact
    for operators who never opt into activity probing.
    """
    payload: list[dict[str, object]] = []
    for row in rows:
        entry = asdict(row)
        if entry.get("last_activity_ts") is None:
            entry.pop("last_activity_ts", None)
        payload.append(entry)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _yes_no(value: bool) -> str:
    """Render a boolean as ``"yes"`` / ``"no"``."""
    return "yes" if value else "no"


def _truncate(value: str, max_len: int) -> str:
    """Truncate ``value`` to ``max_len`` characters, appending ``…`` when cut."""
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"
