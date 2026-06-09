"""``opshub slack cursor`` subcommand implementations (Phase 22-E, ADR-0038).

Operator-facing cursor inspection / surgery for the Slack connector's
compound resume cursor (``channels`` / ``backfill`` / ``threads`` axes):

* ``show`` — pretty-print the current cursor (read-only).
* ``reset`` — drop selected channels' cursor entries so they cold-start
  on the next sync. The **working** replacement for the long-documented
  (but non-functional) ``opshub projections rebuild`` reset path —
  rebuild replays ``ConnectorSyncCompleted`` and restores the same
  cursor, so it never reset anything (ADR-0038 §Context).
* ``backfill`` — fetch an explicit ``(since, until]`` window for one
  channel and advance its low-water mark. The primary rescue for
  pre-feature channels whose historical low-water is unrecorded
  (ADR-0038 §(e) §(f)).

Module-level imports are restricted to ``__future__`` / stdlib / typer so
``opshub --help`` cold start stays under the ADR-0001 budget; heavy
imports (wiring, connector, config) happen inside the functions.
"""

from __future__ import annotations

import json
from typing import Any

import typer

#: The connector key the cursor lives under in ``connector_cursors``.
_CONNECTOR = "slack"

#: Output formats accepted by ``opshub slack cursor show``.
SHOW_FORMAT_CHOICES = ("table", "json")


def _empty_compound() -> dict[str, dict[str, str | None]]:
    """Return the empty 3-axis compound shape (no cursor persisted yet)."""
    return {"channels": {}, "backfill": {}, "threads": {}}


def render_cursor_show(*, output_format: str) -> str:
    """Render the current Slack compound cursor for ``cursor show``.

    Reads ``connector_cursors`` via the source service and parses it with
    the connector's own ``_load_cursors`` (so a legacy / hand-edited
    cursor surfaces the same :class:`ConfigError` the sync path would).
    ``json`` emits the parsed compound; ``table`` emits one ``axis`` block
    per line with sorted entries.
    """
    from opshub.cli._wiring import build_source_service
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    source = build_source_service(actor="cli:slack-cursor")
    raw = source.cursor_get(_CONNECTOR)
    state: dict[str, Any] = _empty_compound() if raw is None else dict(_load_cursors(raw))

    if output_format == "json":
        return json.dumps(state, indent=2, sort_keys=True)

    lines: list[str] = []
    for axis in ("channels", "backfill", "threads"):
        entries: dict[str, str | None] = state[axis]
        lines.append(f"[{axis}] ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})")
        for key in sorted(entries):
            lines.append(f"  {key} = {entries[key]}")
    if raw is None:
        lines.append("")
        lines.append("(no cursor persisted yet — slack has not been synced)")
    return "\n".join(lines)


def run_cursor_reset(*, channels: list[str] | None, reset_all: bool) -> tuple[int, str]:
    """Drop cursor entries for ``channels`` (or all) and persist the result.

    Emits a ``ConnectorSyncCompleted`` with the trimmed compound cursor so
    the projection actually reflects the reset (unlike ``opshub
    projections rebuild``, which would replay the old value back). Returns
    ``(removed_count, new_cursor_json)``.

    ``reset_all`` clears every axis (full cold-start). Otherwise each
    listed channel id is removed from the ``channels`` and ``backfill``
    axes, plus any ``threads`` entry whose ``"{channel_id}:{thread_ts}"``
    key belongs to it.

    The ``reset_all`` path is **``_load_cursors``-free** on purpose: it
    overwrites the whole cursor with the empty compound, so parsing the
    prior value is pointless — and a pre-Phase-20-B flat-dict (or any
    hand-edited shape ``_load_cursors`` would reject) cannot be the source
    of a ``ConfigError`` here. This is the working recovery path
    ([#531](https://github.com/ozzy-labs/opshub/issues/531)) the flat-dict
    sync error points at: persisting the empty compound through
    ``cursor_set`` records a ``ConnectorSyncCompleted`` whose
    ``cursor_value`` is the empty compound, so even ``opshub projections
    rebuild`` (which replays that event) restores the empty compound rather
    than regenerating the flat dict. The selective per-channel path still
    parses (it has to know which entries to trim); a flat-dict cursor has
    no selectable structure, so its only escape hatch is ``--all``.
    """
    from opshub.cli._wiring import build_source_service
    from opshub.connectors.slack.connector import (
        _dump_cursors,  # pyright: ignore[reportPrivateUsage]
        _empty_state,  # pyright: ignore[reportPrivateUsage]
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    source = build_source_service(actor="cli:slack-cursor-reset")
    raw = source.cursor_get(_CONNECTOR)
    if raw is None:
        return 0, _dump_cursors(_empty_state())

    if reset_all:
        # Hard-drop: never call ``_load_cursors`` here. ``-1`` signals
        # "count unknown" (we did not parse the prior cursor), which the
        # CLI renders as "all" rather than a concrete entry count.
        empty = _dump_cursors(_empty_state())
        source.cursor_set(_CONNECTOR, empty, sync_started=False)
        return -1, empty

    # Selective per-channel trim. Must parse to know which entries to drop;
    # a flat-dict cursor (no selectable structure) is unreachable here
    # because the sync error steers flat-dict recovery to ``--all`` above.
    state = _load_cursors(raw)
    targets = set(channels or [])
    removed = 0
    for channel_id in targets:
        if channel_id in state["channels"]:
            del state["channels"][channel_id]
            removed += 1
        state["backfill"].pop(channel_id, None)
        # Drop the channel's thread entries ("{channel_id}:{thread_ts}").
        for key in [k for k in state["threads"] if k.split(":", 1)[0] == channel_id]:
            del state["threads"][key]

    new_value = _dump_cursors(state)
    source.cursor_set(_CONNECTOR, new_value, sync_started=False)
    return removed, new_value


def run_cursor_backfill(*, channel_id: str, since: str, until: str | None) -> int:
    """Backfill one channel's ``(since, until]`` window; return observed count.

    Resolves ``since`` / ``until`` through the shared ``parse_since`` grammar
    (relative ``"30d"`` or ISO date). ``until`` defaults to the channel's
    recorded low-water mark; when there is none (a pre-feature channel),
    the operator must pass ``--until`` explicitly (their old floor). Drives
    :meth:`SlackConnector.backfill_channel` and persists the advanced cursor.
    """
    from opshub.cli._wiring import build_source_service
    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.slack.connector import (
        SlackConnector,
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )
    from opshub.core.errors import ConfigError
    from opshub.core.logging import get_logger
    from opshub.core.time import parse_since, since_to_ts

    since_ts = since_to_ts(parse_since(since, field="--since"))

    source = build_source_service(actor="cli:slack-cursor-backfill")
    prior = source.cursor_get(_CONNECTOR)
    state = _load_cursors(prior) if prior is not None else _empty_compound()

    if until is not None:
        until_ts = since_to_ts(parse_since(until, field="--until"))
    else:
        recorded = state["backfill"].get(channel_id)
        if recorded is None:
            raise ConfigError(
                f"no recorded low-water mark for channel {channel_id!r}; pass "
                "--until explicitly (the floor the channel was last synced "
                "from) so the backfill window is bounded. See ADR-0038 §(f)."
            )
        until_ts = recorded

    if float(since_ts) >= float(until_ts):
        raise ConfigError(
            f"--since ({since!r}) must be strictly older than --until "
            f"(resolved {until_ts!r}); nothing to backfill otherwise."
        )

    context = ConnectorContext(
        source_service=source,
        cursor_value=prior,
        secrets=None,
        logger=get_logger().bind(connector=_CONNECTOR),
    )
    result = SlackConnector().backfill_channel(
        context,
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
    )
    source.cursor_set(_CONNECTOR, result.new_cursor, sync_started=False)
    return result.observed_count


def parse_show_format(value: str) -> str:
    """Validate the ``--format`` value for ``cursor show`` (raises on miss)."""
    if value not in SHOW_FORMAT_CHOICES:
        raise typer.BadParameter(
            f"unknown --format value {value!r}; choose one of {', '.join(SHOW_FORMAT_CHOICES)}",
            param_hint="--format",
        )
    return value
