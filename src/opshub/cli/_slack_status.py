"""``opshub slack status`` — operator-facing Slack sync status (Phase 23-F, #536).

The daily-driver face of the Slack resume cursor. It translates the
per-workspace compound cursor (Phase 24-C envelope:
``{"workspaces": {alias: {channels / backfill / threads / team_id}}}``,
[ADR-0041](../../docs/adr/0041-slack-multi-workspace.md) §(d)) into human
terms, one block per configured workspace, **without claiming contiguous
coverage**.

The cursor is a *resume* cursor, not a *coverage ledger*. It records, per
workspace and channel, a forward high-water mark (``channels``), a backfill
low-water mark (``backfill``), and per-thread late-reply marks (``threads``).
A single low-water per channel cannot represent gap-backfill holes, and Slack
has no delta API for thread late-replies — so a quiet window is
indistinguishable from an unfetched one. ``status`` therefore prints the
high-water and low-water as *separate facts* (``前進取得済み`` /
``過去取得下限``) and never asserts a continuous ``X〜Y`` covered range. The
one gap-ish signal it *can* state precisely is "the next sync will re-fetch
history" — when the effective floor sits below the recorded low-water (the
connector's own gap-backfill trigger, mirrored here). The raw per-workspace
dump lives behind ``--verbose``; the mutation / recovery verbs stay on
``opshub slack cursor`` (reset / backfill).

Module-level imports are restricted to ``__future__`` / stdlib / typer so
``opshub --help`` cold start stays under the ADR-0001 budget; heavy imports
(wiring, connector, config, projection) happen inside the functions.
"""

from __future__ import annotations

import json
from typing import Any

import typer

#: The connector key the cursor lives under in ``connector_cursors``.
_CONNECTOR = "slack"

#: Output formats accepted by ``opshub slack status``.
STATUS_FORMAT_CHOICES = ("table", "json")

#: Axis names rendered per workspace (in this order) by ``--verbose``.
_AXES = ("channels", "backfill", "threads")


def parse_status_format(value: str) -> str:
    """Validate the ``--format`` value for ``status`` (raises on miss)."""
    if value not in STATUS_FORMAT_CHOICES:
        raise typer.BadParameter(
            f"unknown --format value {value!r}; choose one of {', '.join(STATUS_FORMAT_CHOICES)}",
            param_hint="--format",
        )
    return value


def _ts_to_date(ts: str | None) -> str | None:
    """Render a Slack epoch-float ts string as a UTC ``YYYY-MM-DD`` date.

    Returns ``None`` for ``None`` input; falls back to the raw string for a
    malformed ts (defensive — the cursor should only ever hold Slack ts).
    """
    if ts is None:
        return None
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ts


def _channel_names(engine_factory: Any, team_id: str | None) -> dict[str, str]:
    """Best-effort ``{channel_id: channel_name}`` from the demand digest.

    Offline, optional sugar so ``status`` can print ``#general`` next to the
    id. Only channels that have produced a mention / DM demand appear in the
    digest, so a missing entry is normal — the caller falls back to the id.
    Any error (table absent on a fresh DB, etc.) degrades to an empty map.

    Phase 24-D ([ADR-0041](../../docs/adr/0041-slack-multi-workspace.md)
    §(g), issue #556): the digest row key is ``(team_id, channel_id,
    demand_kind)``, so the lookup filters on the workspace block's bound
    ``team_id`` — a channel id that collides across workspaces can no
    longer resolve to the *other* workspace's name. An unbound workspace
    (``team_id is None`` — never synced) has no digest rows by
    construction, so the lookup short-circuits to an empty map.
    """
    if team_id is None:
        return {}
    try:
        from sqlalchemy import select

        from opshub.projections.slack_demand_digest import slack_demand_digest_table

        engine = engine_factory()
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    select(
                        slack_demand_digest_table.c.channel_id,
                        slack_demand_digest_table.c.channel_name,
                    ).where(slack_demand_digest_table.c.team_id == team_id)
                ).all()
        finally:
            engine.dispose()
    except Exception:
        # Name resolution is best-effort sugar — any failure (table absent on
        # a fresh DB, etc.) degrades to ids rather than failing ``status``.
        return {}
    return {row[0]: row[1] for row in rows if row[1]}


def render_status(*, output_format: str, verbose: bool, workspace: str | None = None) -> str:
    """Render the Slack sync status (daily view) or raw dump (``--verbose``).

    Reads ``connector_cursors`` via the source service and parses it with the
    connector's own ``_load_cursors`` (so a legacy / hand-edited cursor
    surfaces the same :class:`ConfigError` the sync path would). The daily view
    additionally reads ``[connectors.slack]`` config to list configured-but-
    unsynced channels and predict the next sync's gap backfill. Phase 24-C:
    one block per configured workspace; ``workspace`` narrows the view to a
    single alias (ADR-0041 §(f)).
    """
    from opshub.cli._wiring import build_source_service
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError

    source = build_source_service(actor="cli:slack-status")
    raw = source.cursor_get(_CONNECTOR)
    if raw is None:
        states: dict[str, dict[str, Any]] = {}
    else:
        loaded = _load_cursors(raw)
        states = {
            alias: {
                "channels": dict(entry["channels"]),
                "backfill": dict(entry["backfill"]),
                "threads": dict(entry["threads"]),
                "team_id": entry["team_id"],
            }
            for alias, entry in loaded["workspaces"].items()
        }

    slack = OpsHubSettings().connectors.slack
    # Render every configured alias plus any cursor-only orphan (an alias
    # removed from config but still carrying state — visible so the
    # operator knows ``cursor reset --workspace <alias> --all`` is the
    # cleanup verb).
    aliases = sorted(set(slack.workspaces) | set(states))
    if workspace is not None:
        if workspace not in aliases:
            raise ConfigError(
                f"unknown Slack workspace alias {workspace!r}; configured "
                f"workspaces: {', '.join(sorted(slack.workspaces)) or '(none)'}"
            )
        aliases = [workspace]

    if verbose:
        return _render_verbose(
            aliases, states, raw_present=raw is not None, output_format=output_format
        )

    blocks: dict[str, dict[str, Any]] = {}
    for alias in aliases:
        state = states.get(alias)
        rows, pending = _build_rows(alias, state, slack)
        blocks[alias] = {
            "channels": rows,
            "pending_backfill": pending,
            "bound_team_id": state["team_id"] if state is not None else None,
            "configured": alias in slack.workspaces,
        }

    if output_format == "json":
        return json.dumps(
            {"workspaces": blocks, "synced": raw is not None},
            indent=2,
            sort_keys=True,
        )
    return _render_table(aliases, blocks, raw_present=raw is not None)


def _build_rows(
    alias: str,
    state: dict[str, Any] | None,
    slack: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute one workspace's status rows + the pending-backfill prediction.

    Mirrors the connector's own gap-backfill trigger so the prediction can
    never drift from what the next sync actually does: a channel with a
    recorded low-water whose effective floor sits *below* it (and backfill is
    enabled) will re-fetch the newly-uncovered window on the next sync.
    Phase 24-C: the floor resolves 3-step (channel ``since`` → workspace
    ``sync_since`` → connector-wide ``sync_since``, ADR-0041 §(c)).
    """
    from opshub.cli._wiring import build_engine
    from opshub.connectors.slack.connector import (
        _EPOCH_TS,  # pyright: ignore[reportPrivateUsage]
        _resolve_floors,  # pyright: ignore[reportPrivateUsage]
        _ts_lt,  # pyright: ignore[reportPrivateUsage]
    )

    workspace = slack.workspaces.get(alias)
    specs = list(workspace.channels) if workspace is not None else []
    default_since = (
        workspace.sync_since
        if workspace is not None and workspace.sync_since is not None
        else slack.sync_since
    )
    floors = _resolve_floors(specs, default_since)
    backfill_enabled = slack.backfill_on_floor_lower
    # Phase 24-D: name lookup is scoped to this workspace's bound
    # team_id (None = never synced = no digest rows to consult).
    names = _channel_names(build_engine, state["team_id"] if state is not None else None)

    channels_axis: dict[str, str | None] = state["channels"] if state is not None else {}
    backfill_axis: dict[str, str | None] = state["backfill"] if state is not None else {}
    threads_axis: dict[str, str | None] = state["threads"] if state is not None else {}

    rows: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for spec in specs:
        ch = spec.id
        high = channels_axis.get(ch)
        low = backfill_axis.get(ch)
        synced = ch in channels_axis
        thread_count = sum(1 for key in threads_axis if key.split(":", 1)[0] == ch)
        rows.append(
            {
                "channel_id": ch,
                "name": names.get(ch),
                "forward_through": _ts_to_date(high),
                "backfilled_down_to": _ts_to_date(low) if low is not None else None,
                "thread_count": thread_count,
                "synced": synced,
            }
        )
        if synced and backfill_enabled and low is not None:
            floor = floors.get(ch)
            target_low = floor if floor is not None else _EPOCH_TS
            if _ts_lt(target_low, low):
                pending.append({"channel_id": ch, "floor": _ts_to_date(target_low)})

    return rows, pending


def _render_table(
    aliases: list[str],
    blocks: dict[str, dict[str, Any]],
    *,
    raw_present: bool,
) -> str:
    """Render the human ``status`` view (the daily operator face)."""
    lines: list[str] = [f"Slack 取得状況 ({len(aliases)} workspace)"]
    if not aliases:
        lines.append("")
        lines.append(
            "(configured workspaces なし — [connectors.slack.workspaces.<alias>] が未設定)"
        )
        return "\n".join(lines)

    for alias in aliases:
        block = blocks[alias]
        bound_team = block["bound_team_id"]
        # Phase 23-H → 24-C: the bound workspace, per alias. Complements
        # `auth test --workspace <alias>` (which shows the *live*
        # workspace the token resolves to now): a mismatch between the
        # two is exactly what the next sync's per-alias bind guard
        # rejects, so surfacing both makes that failure diagnosable.
        bound_text = (
            f"team_id={bound_team}"
            if bound_team is not None
            else "未 bind (次回 sync で bind — ADR-0041)"
        )
        lines.append("")
        lines.append(f"=== workspace: {alias} — {bound_text}")
        if not block["configured"]:
            lines.append(
                "  (config に存在しない cursor 残骸 — 整理は "
                f"`opshub slack cursor reset --workspace {alias} --all`)"
            )
            continue
        rows = block["channels"]
        if not rows:
            lines.append(f"  (channels なし — [connectors.slack.workspaces.{alias}] channels が空)")
            continue
        for row in rows:
            label = f"#{row['name']} ({row['channel_id']})" if row["name"] else row["channel_id"]
            lines.append("")
            lines.append(f"  {label}")
            if not row["synced"]:
                lines.append("    未取得 (cursor entry なし — 次回 sync で初回取得)")
                continue
            lines.append(f"    前進取得済み:   〜{row['forward_through']}    (high-water)")
            low = row["backfilled_down_to"]
            low_text = (
                f"{low}      (low-water)" if low is not None else "先頭まで         (full backfill)"
            )
            lines.append(f"    過去取得下限:   {low_text}")
            lines.append(f"    追跡中スレッド: {row['thread_count']}")
        pending = block["pending_backfill"]
        if pending:
            lines.append("")
            for item in pending:
                lines.append(
                    f"  ⚠ 次回 sync で過去取り直し予定: {item['channel_id']} "
                    f"(floor {item['floor']} < 取得下限)"
                )

    lines.append("")
    lines.append("注: cursor は取得「再開点」であり連続被覆は保証しない。")
    lines.append("    gap backfill 由来の穴は記録しないため status では判定不能。")
    lines.append("    生 cursor + raw ts は `--verbose` / 復旧操作は `opshub slack cursor`。")
    if not raw_present:
        lines.append("")
        lines.append("(no cursor persisted yet — slack has not been synced)")
    return "\n".join(lines)


def _render_verbose(
    aliases: list[str],
    states: dict[str, dict[str, Any]],
    *,
    raw_present: bool,
    output_format: str,
) -> str:
    """Render the raw per-workspace cursor dump (the former ``cursor show``)."""
    if output_format == "json":
        return json.dumps(
            {"workspaces": {alias: states.get(alias) for alias in aliases}},
            indent=2,
            sort_keys=True,
        )

    lines: list[str] = []
    for alias in aliases:
        state = states.get(alias)
        if lines:
            lines.append("")
        if state is None:
            lines.append(f"=== workspace: {alias} (no cursor entry)")
            continue
        team = state["team_id"]
        lines.append(f"=== workspace: {alias}")
        lines.append(f"[team_id] {team if team is not None else '(unbound)'}")
        for axis in _AXES:
            entries: dict[str, str | None] = state[axis]
            lines.append(f"[{axis}] ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})")
            for key in sorted(entries):
                lines.append(f"  {key} = {entries[key]}")
    if not aliases:
        lines.append("(no workspaces configured and no cursor entries)")
    if not raw_present:
        lines.append("")
        lines.append("(no cursor persisted yet — slack has not been synced)")
    return "\n".join(lines)
