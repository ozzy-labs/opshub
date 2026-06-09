"""``opshub slack status`` — operator-facing Slack sync status (Phase 23-F, #536).

The daily-driver face of the Slack resume cursor. It translates the 3-axis
compound cursor (``channels`` / ``backfill`` / ``threads``) into human terms,
**without claiming contiguous coverage**.

The cursor is a *resume* cursor, not a *coverage ledger*. It records, per
channel, a forward high-water mark (``channels``), a backfill low-water mark
(``backfill``), and per-thread late-reply marks (``threads``). A single
low-water per channel cannot represent gap-backfill holes, and Slack has no
delta API for thread late-replies — so a quiet window is indistinguishable
from an unfetched one. ``status`` therefore prints the high-water and
low-water as *separate facts* (``前進取得済み`` / ``過去取得下限``) and never
asserts a continuous ``X〜Y`` covered range. The one gap-ish signal it *can*
state precisely is "the next sync will re-fetch history" — when the effective
floor sits below the recorded low-water (the connector's own gap-backfill
trigger, mirrored here). The raw 3-axis + raw ts dump lives behind
``--verbose``; the mutation / recovery verbs stay on ``opshub slack cursor``
(reset / backfill).

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


def _channel_names(engine_factory: Any) -> dict[str, str]:
    """Best-effort ``{channel_id: channel_name}`` from the demand digest.

    Offline, optional sugar so ``status`` can print ``#general`` next to the
    id. Only channels that have produced a mention / DM demand appear in the
    digest, so a missing entry is normal — the caller falls back to the id.
    Any error (table absent on a fresh DB, etc.) degrades to an empty map.
    """
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
                    )
                ).all()
        finally:
            engine.dispose()
    except Exception:
        # Name resolution is best-effort sugar — any failure (table absent on
        # a fresh DB, etc.) degrades to ids rather than failing ``status``.
        return {}
    return {row[0]: row[1] for row in rows if row[1]}


def render_status(*, output_format: str, verbose: bool) -> str:
    """Render the Slack sync status (daily view) or raw 3-axis dump (``--verbose``).

    Reads ``connector_cursors`` via the source service and parses it with the
    connector's own ``_load_cursors`` (so a legacy / hand-edited cursor
    surfaces the same :class:`ConfigError` the sync path would). The daily view
    additionally reads ``[connectors.slack]`` config to list configured-but-
    unsynced channels and predict the next sync's gap backfill.
    """
    from opshub.cli._wiring import build_source_service
    from opshub.connectors.slack.connector import (
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    source = build_source_service(actor="cli:slack-status")
    raw = source.cursor_get(_CONNECTOR)
    state: dict[str, dict[str, str | None]]
    # Phase 23-H (#538, ADR-0039): the workspace this cursor is bound to.
    # ``None`` = unbound (never synced, or after ``cursor reset --all``).
    bound_team: str | None = None
    if raw is None:
        state = {"channels": {}, "backfill": {}, "threads": {}}
    else:
        loaded = _load_cursors(raw)
        state = {
            "channels": dict(loaded["channels"]),
            "backfill": dict(loaded["backfill"]),
            "threads": dict(loaded["threads"]),
        }
        bound_team = loaded["team_id"]

    if verbose:
        return _render_verbose(
            state, raw_present=raw is not None, output_format=output_format, bound_team=bound_team
        )

    rows, pending = _build_rows(state)

    if output_format == "json":
        return json.dumps(
            {
                "channels": rows,
                "pending_backfill": pending,
                "synced": raw is not None,
                "bound_team_id": bound_team,
            },
            indent=2,
            sort_keys=True,
        )
    return _render_table(rows, pending, raw_present=raw is not None, bound_team=bound_team)


def _build_rows(
    state: dict[str, dict[str, str | None]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute per-channel status rows + the pending-backfill prediction.

    Mirrors the connector's own gap-backfill trigger so the prediction can
    never drift from what the next sync actually does: a channel with a
    recorded low-water whose effective floor sits *below* it (and backfill is
    enabled) will re-fetch the newly-uncovered window on the next sync.
    """
    from opshub.cli._wiring import build_engine
    from opshub.connectors.slack.connector import (
        _EPOCH_TS,  # pyright: ignore[reportPrivateUsage]
        _resolve_floors,  # pyright: ignore[reportPrivateUsage]
        _ts_lt,  # pyright: ignore[reportPrivateUsage]
    )
    from opshub.core.config import OpsHubSettings

    slack = OpsHubSettings().connectors.slack
    specs = list(slack.channels)
    floors = _resolve_floors(specs, slack.sync_since)
    backfill_enabled = slack.backfill_on_floor_lower
    names = _channel_names(build_engine)

    channels_axis = state["channels"]
    backfill_axis = state["backfill"]
    threads_axis = state["threads"]

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
    rows: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    *,
    raw_present: bool,
    bound_team: str | None = None,
) -> str:
    """Render the human ``status`` view (the daily operator face)."""
    # Phase 23-H (#538, ADR-0039): the bound workspace. Complements `auth
    # test` (which shows the *live* workspace the token resolves to now): a
    # mismatch between the two is exactly what the next sync's bind guard
    # rejects, so surfacing both makes that failure diagnosable.
    bound_text = (
        f"bound workspace: team_id={bound_team}"
        if bound_team is not None
        else "bound workspace: 未取得 (次回 sync で bind — ADR-0039)"
    )
    lines: list[str] = [f"Slack 取得状況 ({len(rows)} channel) — {bound_text}"]
    if not rows:
        lines.append("")
        lines.append("(configured channels なし — [connectors.slack] channels が空)")
        return "\n".join(lines)

    for row in rows:
        label = f"#{row['name']} ({row['channel_id']})" if row["name"] else row["channel_id"]
        lines.append("")
        lines.append(label)
        if not row["synced"]:
            lines.append("  未取得 (cursor entry なし — 次回 sync で初回取得)")
            continue
        lines.append(f"  前進取得済み:   〜{row['forward_through']}    (high-water)")
        low = row["backfilled_down_to"]
        low_text = (
            f"{low}      (low-water)" if low is not None else "先頭まで         (full backfill)"
        )
        lines.append(f"  過去取得下限:   {low_text}")
        lines.append(f"  追跡中スレッド: {row['thread_count']}")

    if pending:
        lines.append("")
        for item in pending:
            lines.append(
                f"⚠ 次回 sync で過去取り直し予定: {item['channel_id']} "
                f"(floor {item['floor']} < 取得下限)"
            )

    lines.append("")
    lines.append("注: cursor は取得「再開点」であり連続被覆は保証しない。")
    lines.append("    gap backfill 由来の穴は記録しないため status では判定不能。")
    lines.append("    生 3 軸 + raw ts は `--verbose` / 復旧操作は `opshub slack cursor`。")
    if not raw_present:
        lines.append("")
        lines.append("(no cursor persisted yet — slack has not been synced)")
    return "\n".join(lines)


def _render_verbose(
    state: dict[str, dict[str, str | None]],
    *,
    raw_present: bool,
    output_format: str,
    bound_team: str | None = None,
) -> str:
    """Render the raw 3-axis cursor dump (the former ``cursor show`` content)."""
    if output_format == "json":
        return json.dumps({**state, "team_id": bound_team}, indent=2, sort_keys=True)

    lines: list[str] = [f"[team_id] {bound_team if bound_team is not None else '(unbound)'}"]
    for axis in ("channels", "backfill", "threads"):
        entries: dict[str, str | None] = state[axis]
        lines.append(f"[{axis}] ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})")
        for key in sorted(entries):
            lines.append(f"  {key} = {entries[key]}")
    if not raw_present:
        lines.append("")
        lines.append("(no cursor persisted yet — slack has not been synced)")
    return "\n".join(lines)
