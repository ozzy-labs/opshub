"""Formatters + driver for ``opshub connector slack channels`` (#341 PR2).

PR1 ([#344](https://github.com/ozzy-labs/opshub/pull/344)) shipped the
:func:`opshub.connectors.slack.channels.list_channels` iterator that
walks Slack's ``conversations.list`` API. This module wraps that
iterator into the operator-facing CLI surface — table / TOML / JSON
output, plus a couple of small text helpers — so the CLI handler in
:mod:`opshub.cli.connector` stays as a thin Typer entry point.

The helper lives behind a ``_`` prefix so the static cold-start guard
(``tests/integration/test_cli_imports``) does not require its
module-level imports to stay inside the whitelist (the parametrised
test only walks public ``cli/*.py`` modules). The public
:mod:`opshub.cli.connector` module still defers ``_slack_channels``
import inside the command callback to preserve the ADR-0001 cold-start
budget for operators who never run ``connector slack channels``.

Output formats
--------------

* ``table`` (default): five fixed-width columns ``ID``, ``NAME``,
  ``PRIVATE``, ``ARCHIVED``, ``PURPOSE``. Operators copy the ``ID``
  column into ``opshub.toml``'s ``[connectors.slack] channels = [...]``
  list. The ``PURPOSE`` column is truncated at
  :data:`_PURPOSE_TRUNCATE_LEN` so a single chatty channel does not
  blow out the layout — the JSON output retains the full string.
* ``toml``: emits a TOML snippet ready to paste into ``opshub.toml``.
  Each channel id is on its own line with a trailing comment carrying
  the channel name (and ``(private)`` / ``(archived)`` flags when
  applicable). The header comment records the count so reviewers can
  spot an obviously-empty / truncated paste at a glance.
* ``json``: a JSON array of objects matching
  ``SlackChannel.__dataclass_fields__`` 1:1. Suitable for ``jq`` /
  ``yq`` post-processing or piping into a future `opshub.toml`
  rewriter (deferred per #341 §Out of scope).

Empty result handling
---------------------

Zero matches is **not** an error — operators rerun ``--filter`` /
``--limit`` knobs until they find what they want. The CLI handler
emits a stderr hint (``no channels matched (filter: ...)``) for
table / TOML so the operator sees the empty state explicitly while
keeping stdout free of noise for pipelines; the JSON format still
emits ``[]`` on stdout so ``jq`` consumers see a parseable empty
array.

Token safety
------------

The Slack OAuth token never appears in any output here — the helper
operates on :class:`SlackChannel` rows that the upstream iterator
already stripped down to documented public fields (``id`` / ``name``
/ ``is_private`` / ``is_archived`` / ``purpose``). Error paths bubble
up :class:`opshub.core.errors.ConfigError` /
:class:`opshub.core.errors.ConnectorFailedError` whose messages are
sanitised by the upstream iterator + auth resolver.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

import typer

if TYPE_CHECKING:
    from collections.abc import Iterable

    from opshub.connectors.slack.channels import SlackChannel


__all__ = ["FORMAT_CHOICES", "OutputFormat", "render_channels", "run_channels_command"]


#: Output format literal — kept in lock-step with the Typer
#: ``--format`` choice list :data:`FORMAT_CHOICES` so the CLI surface
#: and the formatter dispatch cannot drift.
OutputFormat = Literal["table", "toml", "json"]

#: Valid ``--format`` values surfaced to Typer. Tuple shape lets
#: tests assert the exact accept-list without re-typing the literals.
FORMAT_CHOICES: tuple[OutputFormat, ...] = ("table", "toml", "json")

#: Truncation cap for the ``PURPOSE`` column in the table output.
#: 40 chars keeps the row under ~120 columns on a typical terminal
#: (id 12 + name 24 + private 8 + archived 9 + purpose 40 + 4 spacers
#: ≈ 97). The JSON format never truncates.
_PURPOSE_TRUNCATE_LEN = 40

#: Minimum column width for the human-readable ``NAME`` column. Slack
#: channel names cap at 80 chars, but most workspaces stay around
#: 20-30 — we set the floor at 16 so the header alone does not span
#: more than the widest channel name in a small workspace.
_NAME_MIN_WIDTH = 16

#: Minimum column width for the ``ID`` column. Slack channel ids are
#: 9-11 chars (``"C..."`` / ``"G..."`` / ``"D..."``); the floor at 11
#: keeps the header aligned with the value column on workspaces that
#: only have short ids.
_ID_MIN_WIDTH = 11


def run_channels_command(
    *,
    output_format: OutputFormat,
    filter_substring: str | None,
    limit: int | None,
    include_private: bool,
    include_archived: bool,
) -> None:
    """Drive the ``opshub connector slack channels`` command end-to-end.

    This is the seam :mod:`opshub.cli.connector` calls — it owns the
    auth construction, the channel listing call, the empty-result hint
    (stderr), and the formatted output (stdout). The handler in
    ``connector.py`` is intentionally thin so the lazy-import
    bookkeeping stays in one place and this helper covers the
    operator-visible behaviour.

    Parameters
    ----------
    output_format:
        One of :data:`FORMAT_CHOICES`.
    filter_substring:
        Case-insensitive substring match against ``channel.name``.
        ``None`` (or the empty string) disables filtering.
    limit:
        Maximum number of channels to yield. ``None`` means no cap.
    include_private:
        When ``True``, ``"private_channel"`` is added to Slack's
        ``types`` request parameter (requires ``groups:read`` scope).
    include_archived:
        When ``True``, archived channels are included; default
        excludes them client-side.

    Raises
    ------
    opshub.core.errors.ConfigError
        Bubbled up from :class:`SlackAuth` (no token / wrong prefix)
        or from the SDK extras gate.
    opshub.core.errors.ConnectorFailedError
        Bubbled up from :func:`list_channels` (invalid_auth /
        missing_scope / exhausted 429 retries).
    """
    # Lazy-import the Slack subpackage so ``opshub --help`` cold start
    # never pays for ``slack_sdk`` (ADR-0001). The auth module pulls
    # only :mod:`opshub.core.errors` at module load; the channels
    # module pulls :mod:`opshub.core.errors` and (lazily) ``slack_sdk``
    # inside :func:`list_channels`.
    from opshub.connectors.slack.auth import SlackAuth
    from opshub.connectors.slack.channels import list_channels

    auth = SlackAuth()
    channels = list_channels(
        auth,
        include_private=include_private,
        include_archived=include_archived,
        filter_substring=filter_substring,
        limit=limit,
    )

    # We need to consume the iterator before formatting so the empty-
    # result hint and the post-yield count are both available. The
    # discovery use case is operator-interactive (paste-and-edit), not
    # streaming, so materialising the list is fine — workspaces with
    # tens of thousands of channels are not the target of this command
    # (use ``--filter`` / ``--limit`` to scope the output instead).
    rows = list(channels)

    if not rows and output_format != "json":
        # Surface the empty-state to stderr so pipelines see only the
        # formatted (empty) stdout. JSON still emits ``[]`` on stdout
        # because ``jq`` consumers expect a parseable array even when
        # zero matches.
        _emit_empty_hint(filter_substring=filter_substring, err=True)
        # Fall through so ``table`` still emits its header (matching the
        # operator's mental model of "ran a command, got an empty
        # table") and ``toml`` emits ``channels = []`` for paste-ability.

    rendered = render_channels(rows, output_format=output_format)
    if rendered:
        typer.echo(rendered)


def render_channels(
    rows: Iterable[SlackChannel],
    *,
    output_format: OutputFormat,
) -> str:
    """Format a stream of :class:`SlackChannel` rows for stdout.

    Pure function (no side effects, no I/O) so unit tests can pass a
    list of fixtures and assert exact bytes — the
    :func:`run_channels_command` wrapper is what shells out to
    ``typer.echo`` and reads from the network.

    Parameters
    ----------
    rows:
        Iterable of :class:`SlackChannel`. Consumed once.
    output_format:
        One of :data:`FORMAT_CHOICES`.

    Returns
    -------
    str
        The formatted output (no trailing newline — ``typer.echo``
        adds one).
    """
    materialised = list(rows)
    if output_format == "table":
        return _render_table(materialised)
    if output_format == "toml":
        return _render_toml(materialised)
    if output_format == "json":
        return _render_json(materialised)
    # ``Literal`` narrowing should prevent this, but a runtime guard
    # gives a clear error if someone bypasses the Typer ``case_sensitive``
    # gate (e.g. by calling :func:`render_channels` directly with a
    # mistyped string).
    raise ValueError(f"unknown output format: {output_format!r}")


# ----- private helpers ---------------------------------------------------


def _emit_empty_hint(*, filter_substring: str | None, err: bool) -> None:
    """Print the ``no channels matched`` stderr hint for empty results.

    Centralising the message keeps the table / TOML empty paths in
    sync and lets tests assert exactly one shape for the hint.
    """
    if filter_substring:
        typer.echo(f"no channels matched (filter: {filter_substring!r})", err=err)
    else:
        typer.echo("no channels matched", err=err)


def _render_table(rows: list[SlackChannel]) -> str:
    """Render channels as a fixed-width 5-column table.

    The column widths grow to accommodate the widest value (with a
    floor so the header is never wider than the values for typical
    short workspaces). ``PURPOSE`` is truncated at
    :data:`_PURPOSE_TRUNCATE_LEN` so a single chatty channel does not
    blow out the layout — JSON keeps the full string for downstream
    consumers.
    """
    id_width = max(_ID_MIN_WIDTH, max((len(row.id) for row in rows), default=0))
    name_width = max(_NAME_MIN_WIDTH, max((len(row.name) for row in rows), default=0))
    # ``PRIVATE`` (7) and ``ARCHIVED`` (8) are fixed-width headers — the
    # value column ("yes" / "no") is always narrower so the header
    # dominates. We hard-code the width to match the header length.
    private_width = 7
    archived_width = 8

    header = (
        f"{'ID':<{id_width}}  "
        f"{'NAME':<{name_width}}  "
        f"{'PRIVATE':<{private_width}}  "
        f"{'ARCHIVED':<{archived_width}}  "
        f"PURPOSE"
    )
    lines = [header]
    for row in rows:
        purpose = _truncate(row.purpose, _PURPOSE_TRUNCATE_LEN)
        lines.append(
            f"{row.id:<{id_width}}  "
            f"{row.name:<{name_width}}  "
            f"{_yes_no(row.is_private):<{private_width}}  "
            f"{_yes_no(row.is_archived):<{archived_width}}  "
            f"{purpose}"
        )
    return "\n".join(lines)


def _render_toml(rows: list[SlackChannel]) -> str:
    """Render channels as a TOML snippet ready to paste into ``opshub.toml``.

    The header comment records the total count so a reviewer can
    spot an obviously-truncated paste. Each channel id sits inside
    string quotes (Slack ids never contain ``"`` so we do not need to
    escape) followed by a comment with the channel name and any
    relevant flags. The shape matches the snippet at #341 §設計
    §出力例 (--format toml).
    """
    header = f"# Slack channels ({len(rows)})"
    if not rows:
        # Empty list still needs to round-trip the assignment so a
        # ``--format toml > snippet.toml`` capture can be sourced
        # without a TOML parse error.
        return f"{header}\nchannels = []"
    lines = [header, "channels = ["]
    for row in rows:
        flags: list[str] = []
        if row.is_private:
            flags.append("private")
        if row.is_archived:
            flags.append("archived")
        if flags:
            comment = f"{row.name} ({', '.join(flags)})"
        else:
            comment = row.name
        lines.append(f'  "{row.id}",  # {comment}')
    lines.append("]")
    return "\n".join(lines)


def _render_json(rows: list[SlackChannel]) -> str:
    """Render channels as a JSON array of dataclass dicts.

    Uses :func:`dataclasses.asdict` so the field order matches the
    dataclass definition (id / name / is_private / is_archived /
    purpose). Indented for human readability — the CLI is for
    interactive paste, not for high-throughput pipelines, and ``jq``
    handles indented or compact input identically.
    """
    payload = [asdict(row) for row in rows]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _yes_no(value: bool) -> str:
    """Render a boolean as ``"yes"`` / ``"no"`` (3-char column-friendly).

    ``True`` / ``False`` are too long for the fixed-width column and
    do not match the lowercase aesthetic of the rest of the
    table. The Slack docs themselves use ``true`` / ``false`` in
    JSON, which the ``--format json`` output preserves verbatim.
    """
    return "yes" if value else "no"


def _truncate(value: str, max_len: int) -> str:
    """Truncate ``value`` to ``max_len`` characters, appending ``…`` when cut.

    The ellipsis is a single-character substitution (not three
    ASCII dots) so the visible width matches the truncated string's
    advertised length — important for the fixed-width table layout.
    """
    if len(value) <= max_len:
        return value
    # Subtract one so the ellipsis sits inside the budget rather than
    # extending past it.
    return value[: max_len - 1] + "…"
