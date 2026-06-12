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
:mod:`opshub.cli.slack` module still defers ``_slack_conversations``
import inside the command callback to preserve the ADR-0001
cold-start budget for operators who never run the ``opshub slack
conversations`` subcommand.

Output formats
--------------

* ``table`` (default): five fixed-width columns ``ID``, ``TYPE``,
  ``NAME / PARTICIPANTS``, ``ARCHIVED``, ``PURPOSE``. The ``NAME /
  PARTICIPANTS`` column shows the channel name for public/private
  channels, the peer name for DMs, and a comma-joined participant
  list (capped at :data:`_MPIM_PARTICIPANT_DISPLAY_LIMIT` with a
  ``+N`` suffix) for MPIMs.
* ``toml``: emits a complete, paste-ready block — the
  ``[connectors.slack]`` header with ``enabled = true`` plus the
  ``[connectors.slack.workspaces.<alias>]`` table carrying the
  ``channels`` array (Phase 24-C, ADR-0041 §(f)) — so the output drops
  straight into ``opshub.toml`` and ``opshub slack sync`` runs with no
  further editing (Phase 23-E, #535). Each id is annotated with a
  comment carrying the conversation type and a human-readable label.
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
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import typer

from opshub.cli import _progress
from opshub.connectors.slack.conversations import (
    CONVERSATION_TYPES,
    ConversationType,
    SortKey,
)
from opshub.core.errors import ValidationError
from opshub.core.time import parse_since as _parse_since_core

if TYPE_CHECKING:
    from collections.abc import Iterable

    from opshub.connectors.slack.conversations import SlackConversation


__all__ = [
    "DEFAULT_SORT",
    "DEFAULT_TYPES_CSV",
    "FORMAT_CHOICES",
    "SORT_CHOICES",
    "OutputFormat",
    "parse_since",
    "parse_types",
    "render_conversations",
    "run_conversations_command",
]


#: Output format literal — kept in lock-step with the Typer
#: ``--format`` choice list :data:`FORMAT_CHOICES`.
OutputFormat = Literal["table", "toml", "json"]

#: Valid ``--format`` values surfaced to Typer. Phase 19-D (ADR-0035
#: §(a)) shifts the default to ``toml`` so the primary paste-into-
#: ``opshub.toml`` workflow stops paying the ``--format toml`` tax;
#: ``table`` stays in the choice list for eyeball / debug use.
FORMAT_CHOICES: tuple[OutputFormat, ...] = ("table", "toml", "json")

#: Valid ``--sort`` values surfaced to Typer (ADR-0035 §(c)). The keys
#: map 1:1 to the populated dataclass field
#: (``last_self_post_ts`` / ``last_activity_ts``) so CLI / JSON / DB
#: schema share one vocabulary. ``name`` is the documented default —
#: alphabetical-within-type listing matches the "browse the workspace
#: and pick channels to sync" use case (ADR-0035 §(b)).
SORT_CHOICES: tuple[SortKey, ...] = ("name", "last_self_post", "last_activity")

#: Phase 19-D default for ``--sort``. Spelled out as a constant (rather
#: than re-derived from ``SORT_CHOICES[0]``) so a future reordering of
#: the choice list does not silently flip the operator-visible default.
DEFAULT_SORT: SortKey = "name"

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


def parse_since(raw: str) -> datetime:
    """Typer-facing wrapper over :func:`opshub.core.time.parse_since`.

    The grammar (relative ``7d`` / ``2w`` + ISO absolute, trailing ``Z``
    accepted, tz-naive defaulted to UTC) and message text live in
    :func:`opshub.core.time.parse_since` so the connector config floor
    (``[connectors.slack] sync_since`` / per-channel ``since``) can reuse
    the identical parser without importing the CLI layer (Phase 20,
    #459 / ADR-0036). This wrapper only translates the core
    :class:`~opshub.core.errors.ValidationError` into a
    :class:`typer.BadParameter` so Typer surfaces the message verbatim
    with exit code 2 — preserving the pre-Phase-20 ``--since`` callback
    contract byte-for-byte (``field`` defaults to ``"--since"``).
    """
    try:
        return _parse_since_core(raw)
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc


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
    workspace: str,
    output_format: OutputFormat,
    filter_substring: str | None,
    limit: int | None,
    types: tuple[ConversationType, ...],
    include_archived: bool,
    all: bool,
    since: datetime | None = None,
    sort: SortKey = DEFAULT_SORT,
) -> None:
    """Drive ``opshub slack conversations`` end-to-end.

    This is the seam :mod:`opshub.cli.slack` calls. The handler in
    ``slack.py`` is intentionally thin so the lazy-import
    bookkeeping stays in one place and this helper covers the
    operator-visible behaviour.

    Parameters
    ----------
    workspace:
        Resolved workspace alias (Phase 24-C,
        [ADR-0041](../../docs/adr/0041-slack-multi-workspace.md) §(f) —
        the ``slack.py`` wrapper applies the shared default rule before
        calling here). Selects the per-alias token slot
        (``connector:slack:<alias>:token``) and the alias the
        ``--format=toml`` block is emitted for.
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
        Optional tz-aware :class:`datetime.datetime` cutoff, applied on
        the ts of the axis selected by ``sort``. Phase 23-G (#537):
        ``--since`` is a pure filter — it never selects an axis on its
        own, so the CLI rejects ``--sort=name`` + ``--since`` (name has no
        activity ts to filter by). When ``sort in ("last_self_post",
        "last_activity")`` and ``since`` is ``None``, the adapter applies
        an implicit ``90d`` cutoff, emits an ADR-0035 §(e) notice, and
        stamps the resolved window into the listing output. The table
        renderer adds a ``LAST_POST`` / ``LAST_ACTIVITY`` column when a
        probe ran.
    sort:
        Sort key + axis selector (ADR-0035 §(c); Phase 23-G #537 made the
        axis selectable *only* by an explicit ``--sort``):

        * ``"name"`` (default) — display_name within type bucket. Never
          probes; ``--since`` is rejected with this sort.
        * ``"last_self_post"`` — engagement axis. One
          ``search.messages`` call returns the operator's own posts;
          channels missing from the index are dropped. Populates
          ``last_self_post_ts``. Requires ``search:read`` on a User
          Token.
        * ``"last_activity"`` — any-author axis. One
          ``conversations.history?limit=1`` per row; channels with any-
          author messages newer than ``since`` survive. Populates
          ``last_activity_ts``. Requires ``*:history`` per type.

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

    auth = SlackAuth(workspace)

    # Resolve the activity-probe axis from the sort key alone (Phase 23-G
    # #537: the axis is selected *only* by an explicit ``--sort``; the
    # former ``sort="name" + --since`` implicit engagement default was
    # removed). ``engagement_probe`` = ``sort="last_self_post"``;
    # ``any_probe`` = ``sort="last_activity"``; ``probe_ran`` controls the
    # ts column / TOML comment / JSON field exposure. ``sort="name"`` never
    # probes (and ``--sort=name`` + ``--since`` is rejected upstream).
    engagement_probe = sort == "last_self_post"
    any_probe = sort == "last_activity"
    probe_ran = engagement_probe or any_probe

    # Wrap the iterator in the indeterminate progress reporter so the
    # operator sees a spinner + page-tick on slow workspaces. ``reporter``
    # is a no-op when progress is disabled (non-TTY / ``--no-progress``),
    # which keeps captured-output tests stable. The spinner description
    # names the activity-probe axis (ADR-0035 §(c)) so an operator
    # watching the spinner can tell whether ``search.messages`` is
    # being walked vs per-row ``conversations.history`` — useful for
    # debugging rate-limit / scope failures.
    warnings: list[str] = []
    # Phase 23-G (#537): single-element sink for the resolved implicit-90d
    # cutoff (populated by ``list_conversations`` only when an explicit
    # ts-axis sort ran without ``--since``) so the renderer can stamp the
    # window into the output, making the default observable in-band.
    resolved_cutoff: list[datetime] = []
    if engagement_probe:
        description = "listing conversations + engagement"
    elif any_probe:
        description = "listing conversations + activity"
    else:
        description = "listing conversations"
    with _progress.indeterminate(description) as reporter:
        conversations = list_conversations(
            auth,
            types=types,
            include_archived=include_archived,
            filter_substring=filter_substring,
            limit=limit,
            all=all,
            since=since,
            sort=sort,
            warnings=warnings,
            resolved_cutoff=resolved_cutoff,
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

    sorted_rows = _sort_rows(rows, sort=sort)

    if not sorted_rows and output_format != "json":
        _emit_empty_hint(filter_substring=filter_substring, err=True)

    # Stamp the resolved implicit-90d cutoff into the output when it fired
    # (Phase 23-G #537), so the window is observable in the artifact the
    # operator reads, not only on the transient stderr notice.
    cutoff_date = resolved_cutoff[0].date().isoformat() if resolved_cutoff else None

    rendered = render_conversations(
        sorted_rows,
        output_format=output_format,
        show_activity=probe_ran,
        engagement_probe=engagement_probe,
        cutoff_date=cutoff_date,
        workspace=workspace,
    )
    if rendered:
        typer.echo(rendered)

    # Next-action hint (Phase 23-E, #535): the toml block is the paste
    # target, so point the operator at the obvious follow-up. Emitted on
    # stderr (after the stdout block) only when there is something to
    # paste, so it never pollutes a piped ``--format=toml`` capture or a
    # ``--format=json`` machine-read path.
    if output_format == "toml" and sorted_rows:
        typer.echo(
            "next: paste the block above into opshub.toml, then run `opshub slack sync`",
            err=True,
        )


def render_conversations(
    rows: Iterable[SlackConversation],
    *,
    output_format: OutputFormat,
    show_activity: bool = False,
    engagement_probe: bool = False,
    cutoff_date: str | None = None,
    workspace: str,
) -> str:
    """Format a stream of :class:`SlackConversation` rows for stdout.

    Pure function (no side effects, no I/O) so unit tests can pass a
    list of fixtures and assert exact bytes — the
    :func:`run_conversations_command` wrapper is what shells out to
    ``typer.echo`` and reads from the network.

    ``show_activity`` controls whether the table / TOML layout includes
    the activity-axis column / comment annotation (default: hidden;
    set to ``True`` whenever the adapter ran a probe — engagement axis
    via ``sort="last_self_post"`` / ``sort="name"`` + ``--since``, or
    any-author axis via ``sort="last_activity"``). ``engagement_probe``
    selects the column header (``LAST_POST`` for engagement axis,
    ``LAST_ACTIVITY`` for the any-author axis) and the TOML comment
    label. The JSON renderer always reflects whichever axis ts is
    populated and drops the key when it is ``None`` so a no-probe
    invocation does not pollute the payload with meaningless null
    fields and a single invocation never emits both ts fields on the
    same row.
    """
    materialised = list(rows)
    if output_format == "table":
        return _render_table(
            materialised,
            show_activity=show_activity,
            engagement_probe=engagement_probe,
            cutoff_date=cutoff_date,
        )
    if output_format == "toml":
        return _render_toml(
            materialised,
            engagement_probe=engagement_probe,
            cutoff_date=cutoff_date,
            workspace=workspace,
        )
    if output_format == "json":
        return _render_json(materialised)
    raise ValueError(f"unknown output format: {output_format!r}")


def _sort_rows(
    rows: Iterable[SlackConversation],
    *,
    sort: SortKey,
) -> list[SlackConversation]:
    """Sort rows by the documented fixed type buckets and within-bucket key.

    Type buckets follow :data:`_TYPE_BUCKET_ORDER` (``public →
    private → mpim → im``); rows of an unrecognised type bucket sort
    last in their original order. Within each bucket the within-bucket
    key depends on ``sort`` (ADR-0035 §(c)):

    * ``"name"`` → ``display_name`` case-insensitive ascending;
      ``id`` is used as a stable tiebreaker so two rows sharing a
      display name keep a deterministic order across runs. Applied
      even when an engagement-axis probe ran (``sort="name"`` +
      ``--since``, ADR-0035 §(d)): the sort key only controls
      ordering, the probe controls filtering / ts column exposure.
    * ``"last_self_post"`` → engagement-axis ts descending; rows
      missing ``last_self_post_ts`` (defensive, should not occur on
      this path) sort last.
    * ``"last_activity"`` → any-author-axis ts descending; rows
      missing ``last_activity_ts`` (defensive) sort last.
    """
    bucket_index = {bucket: idx for idx, bucket in enumerate(_TYPE_BUCKET_ORDER)}
    fallback_bucket = len(_TYPE_BUCKET_ORDER)

    if sort == "name":

        def _name_key(row: SlackConversation) -> tuple[int, str, str]:
            bucket = bucket_index.get(row.type, fallback_bucket)
            return (bucket, row.display_name.lower(), row.id)

        return sorted(rows, key=_name_key)

    if sort == "last_self_post":

        def _self_post_key(row: SlackConversation) -> tuple[int, int, float, str]:
            bucket = bucket_index.get(row.type, fallback_bucket)
            ts = row.last_self_post_ts
            has_ts = 0 if ts is not None else 1
            ts_key = -ts if ts is not None else 0.0
            return (bucket, has_ts, ts_key, row.id)

        return sorted(rows, key=_self_post_key)

    # sort == "last_activity"
    def _activity_key(row: SlackConversation) -> tuple[int, int, float, str]:
        bucket = bucket_index.get(row.type, fallback_bucket)
        ts = row.last_activity_ts
        has_ts = 0 if ts is not None else 1
        ts_key = -ts if ts is not None else 0.0
        return (bucket, has_ts, ts_key, row.id)

    return sorted(rows, key=_activity_key)


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


def _render_table(
    rows: list[SlackConversation],
    *,
    show_activity: bool = False,
    engagement_probe: bool = False,
    cutoff_date: str | None = None,
) -> str:
    """Render conversations as a fixed-width table.

    ``show_activity=True`` inserts an activity-axis column between
    ``ARCHIVED`` and ``PURPOSE``. The header is ``LAST_POST`` for the
    engagement axis (``engagement_probe=True``) and ``LAST_ACTIVITY``
    for the any-author axis; column width is constant at
    :data:`_LAST_ACTIVITY_WIDTH` either way so a TOML / table diff
    across axes stays column-aligned. The column renders the UTC date
    (``YYYY-MM-DD``) of the populated axis ts; rows without a ts
    (defensive, should not occur when the caller asked for a probe)
    render ``-``.
    """
    name_values = [_format_name_column(row) for row in rows]

    id_width = max(_ID_MIN_WIDTH, max((len(row.id) for row in rows), default=0))
    name_width = max(_NAME_MIN_WIDTH, max((len(v) for v in name_values), default=0))
    archived_width = 8

    activity_header = "LAST_POST" if engagement_probe else "LAST_ACTIVITY"

    header_parts = [
        f"{'ID':<{id_width}}",
        f"{'TYPE':<{_TYPE_WIDTH}}",
        f"{'NAME / PARTICIPANTS':<{name_width}}",
        f"{'ARCHIVED':<{archived_width}}",
    ]
    if show_activity:
        header_parts.append(f"{activity_header:<{_LAST_ACTIVITY_WIDTH}}")
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
            row_ts = row.last_self_post_ts if engagement_probe else row.last_activity_ts
            cells.append(f"{_format_activity_date(row_ts):<{_LAST_ACTIVITY_WIDTH}}")
        cells.append(purpose)
        lines.append("  ".join(cells))
    if cutoff_date is not None:
        # Phase 23-G (#537): make the implicit 90d window observable in-band.
        lines.append(f"# activity window: since {cutoff_date} (90d default; pass --since to widen)")
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


def _render_toml(
    rows: list[SlackConversation],
    *,
    engagement_probe: bool = False,
    cutoff_date: str | None = None,
    workspace: str,
) -> str:
    """Render conversations as a paste-ready workspace-table TOML block.

    Phase 23-E (#535): the output is a self-contained section — the
    ``[connectors.slack]`` table header, ``enabled = true``, and the
    ``channels`` array — so an operator can paste it straight into
    ``opshub.toml`` and run ``opshub slack sync`` with no further
    editing. The previous form emitted a *bare* ``channels = [...]``
    array; pasting that without an enclosing section dropped a top-level
    array into whatever table preceded it (or none), silently breaking
    the config. Emitting the header + ``enabled`` flag closes that trap.

    Phase 24-C ([ADR-0041](../../docs/adr/0041-slack-multi-workspace.md)
    §(f)): the ``channels`` array now lives under the
    ``[connectors.slack.workspaces.<alias>]`` table — the flat
    ``[connectors.slack] channels`` key is rejected by the settings
    layer — so the block emits the workspace table for the alias the
    listing ran against (``workspace``, resolved by the shared
    ADR-0041 §(f) default rule in ``slack.py``).

    Each id sits inside string quotes followed by a comment that names
    the conversation type and a human-readable label so reviewers can
    spot DMs / MPIMs / archived entries at a glance before pasting. The
    activity flag label depends on the probe axis: ``"last post
    YYYY-MM-DD"`` for the engagement axis (matches the table header
    label), ``"last YYYY-MM-DD"`` for the any-author axis (preserves the
    #374 wording).
    """
    section = [
        "[connectors.slack]",
        "enabled = true",
        "",
        f"[connectors.slack.workspaces.{workspace}]",
    ]
    header = f"# Slack conversations ({len(rows)})"
    # Phase 23-G (#537): when an explicit ts-axis sort defaulted to the
    # implicit 90d window, stamp the resolved cutoff as a comment so the
    # filter boundary is visible in the pasted block (not just on stderr).
    cutoff_comment = (
        [f"# activity window: since {cutoff_date} (90d default; pass --since to widen)"]
        if cutoff_date is not None
        else []
    )
    if not rows:
        return "\n".join([*section, header, *cutoff_comment, "channels = []"])
    label_prefix = "last post" if engagement_probe else "last"
    lines = [*section, header, *cutoff_comment, "channels = ["]
    for row in rows:
        flags: list[str] = [row.type]
        if row.is_archived:
            flags.append("archived")
        axis_ts = row.last_self_post_ts if engagement_probe else row.last_activity_ts
        if axis_ts is not None:
            # Operators reviewing a TOML paste want the activity
            # signal inline with the channel id; carrying it in the
            # comment keeps the actual config (``"C..."``) untouched
            # while still showing the reviewer "this channel last
            # spoke on 2026-05-30" (or "I last posted there").
            flags.append(f"{label_prefix} {_format_activity_date(axis_ts)}")
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

    ``last_activity_ts`` / ``last_self_post_ts`` are omitted from rows
    where they are ``None`` so a no-probe invocation does not pollute
    the payload with meaningless null fields, and a single ``--sort``
    invocation never carries both axis fields on the same row
    (ADR-0034 §(g) / ADR-0035 §(c)) — keeps the JSON contract of #366
    intact for operators who never opt into activity probing and
    preserves Phase 19-B field disjointness across the engagement-
    axis paths (explicit ``--sort=last_self_post`` and implicit
    ``--sort=name`` + ``--since``).
    """
    payload: list[dict[str, object]] = []
    for row in rows:
        entry = asdict(row)
        for axis_field in ("last_activity_ts", "last_self_post_ts"):
            if entry.get(axis_field) is None:
                entry.pop(axis_field, None)
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
