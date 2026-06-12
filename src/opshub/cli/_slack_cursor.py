"""``opshub slack cursor`` subcommand implementations (Phase 22-E, ADR-0038).

Recovery / low-level cursor surgery (the *mutation* verbs) for the Slack
connector's per-workspace compound resume cursor (Phase 24-C envelope:
``{"workspaces": {alias: {channels / backfill / threads / team_id}}}``,
[ADR-0041](../../docs/adr/0041-slack-multi-workspace.md) §(d)). Read-only
**inspection** lives on ``opshub slack status`` (Phase 23-F, #536) —
including ``status --verbose`` for the raw per-workspace dump:

* ``reset`` — drop selected channels' cursor entries (or a whole
  workspace's entry, or the whole envelope) so they cold-start on the
  next sync. The **working** replacement for the long-documented (but
  non-functional) ``opshub projections rebuild`` reset path — rebuild
  replays ``ConnectorSyncCompleted`` and restores the same cursor, so
  it never reset anything (ADR-0038 §Context).
* ``backfill`` — fetch an explicit ``(since, until]`` window for one
  channel of one workspace and advance its low-water mark. The primary
  rescue for pre-feature channels whose historical low-water is
  unrecorded (ADR-0038 §(e) §(f)).

Workspace resolution (ADR-0041 §(f)): ``--channel`` reset and
``backfill`` resolve the target alias via the shared default rule (one
configured workspace → that one; otherwise ``--workspace`` required —
channel ids can collide across workspaces, so ambiguity is rejected
loud). The global ``--all`` reset stays alias-free and parse-free so it
can recover any legacy / corrupt cursor shape (#531).

Module-level imports are restricted to ``__future__`` / stdlib typing so
``opshub --help`` cold start stays under the ADR-0001 budget; heavy imports
(wiring, connector, config, projection) happen inside the functions.
"""

from __future__ import annotations

from typing import Any

#: The connector key the cursor lives under in ``connector_cursors``.
_CONNECTOR = "slack"


def run_cursor_reset(
    *, channels: list[str] | None, reset_all: bool, workspace: str | None = None
) -> tuple[int, str]:
    """Drop cursor entries and persist the result.

    Emits a ``ConnectorSyncCompleted`` with the trimmed cursor envelope so
    the projection actually reflects the reset (unlike ``opshub
    projections rebuild``, which would replay the old value back). Returns
    ``(removed_count, new_cursor_json)``.

    Three scopes (Phase 24-C, ADR-0041 §(f)):

    * ``reset_all`` without ``workspace`` — overwrite the whole cursor
      with the empty envelope. This path is **parse-free** on purpose:
      it never calls ``_load_cursors``, so a pre-Phase-24 legacy shape
      (or any hand-edited corruption) cannot raise here. This is the
      working recovery path ([#531](https://github.com/ozzy-labs/opshub/issues/531))
      the legacy-cursor sync error points at: persisting the empty
      envelope through ``cursor_set`` records a ``ConnectorSyncCompleted``
      whose ``cursor_value`` is the empty envelope, so even ``opshub
      projections rebuild`` (which replays that event) restores the
      empty envelope rather than regenerating the legacy shape.
    * ``reset_all`` with ``workspace`` — parse the envelope and drop that
      alias's entry entirely (channels + backfill + threads + team_id
      unbind). The parse is required to keep the other aliases intact;
      a corrupt cursor therefore raises here — the escape hatch is the
      global ``--all``.
    * ``channels`` — resolve the target alias (default rule, see module
      docstring) and trim the listed channel ids from its ``channels`` /
      ``backfill`` axes plus any ``threads`` entry whose
      ``"{channel_id}:{thread_ts}"`` key belongs to them.
    """
    from opshub.cli._wiring import build_source_service
    from opshub.connectors.slack.connector import (
        _dump_cursors,  # pyright: ignore[reportPrivateUsage]
        _empty_envelope,  # pyright: ignore[reportPrivateUsage]
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )

    source = build_source_service(actor="cli:slack-cursor-reset")
    raw = source.cursor_get(_CONNECTOR)
    if raw is None:
        return 0, _dump_cursors(_empty_envelope())

    if reset_all and workspace is None:
        # Hard-drop: never call ``_load_cursors`` here. ``-1`` signals
        # "count unknown" (we did not parse the prior cursor), which the
        # CLI renders as "all" rather than a concrete entry count.
        empty = _dump_cursors(_empty_envelope())
        source.cursor_set(_CONNECTOR, empty, sync_started=False)
        return -1, empty

    envelope = _load_cursors(raw)

    if reset_all and workspace is not None:
        # Per-workspace drop (alias resolved = the explicit flag value;
        # an alias absent from the envelope is a no-op reset).
        removed_state = envelope["workspaces"].pop(workspace, None)
        removed = len(removed_state["channels"]) if removed_state is not None else 0
        new_value = _dump_cursors(envelope)
        source.cursor_set(_CONNECTOR, new_value, sync_started=False)
        return removed, new_value

    # Selective per-channel trim within one workspace. Must parse to know
    # which entries to drop; a legacy-shape cursor (no selectable
    # structure) is unreachable here because the sync error steers legacy
    # recovery to the global ``--all`` above.
    from opshub.cli._slack_workspace import resolve_workspace_alias

    alias = resolve_workspace_alias(workspace)
    state = envelope["workspaces"].get(alias)
    if state is None:
        return 0, _dump_cursors(envelope)
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

    new_value = _dump_cursors(envelope)
    source.cursor_set(_CONNECTOR, new_value, sync_started=False)
    return removed, new_value


def run_cursor_backfill(
    *, channel_id: str, since: str, until: str | None, workspace: str | None = None
) -> int:
    """Backfill one channel's ``(since, until]`` window; return observed count.

    Resolves the target workspace alias via the shared default rule
    (ADR-0041 §(f)), then ``since`` / ``until`` through the shared
    ``parse_since`` grammar (relative ``"30d"`` or ISO date). ``until``
    defaults to the channel's recorded low-water mark **within that
    workspace's cursor entry**; for a pre-feature channel with no
    recorded low-water it defaults to the **oldest already-ingested
    message ts** for the channel (Phase 23-F-2, #536; team-scoped when
    the alias is already bound) — the natural upper bound, since history
    below the oldest ingested message is the unfetched window. Only when
    the channel has no ingested messages at all does the operator have
    to pass ``--until`` explicitly. Drives
    :meth:`SlackConnector.backfill_channel` and persists the advanced
    cursor.
    """
    from opshub.cli._slack_workspace import resolve_workspace_alias
    from opshub.cli._wiring import build_source_service
    from opshub.connectors.context import ConnectorContext
    from opshub.connectors.slack.connector import (
        SlackConnector,
        _empty_envelope,  # pyright: ignore[reportPrivateUsage]
        _empty_state,  # pyright: ignore[reportPrivateUsage]
        _load_cursors,  # pyright: ignore[reportPrivateUsage]
    )
    from opshub.core.errors import ConfigError
    from opshub.core.logging import get_logger
    from opshub.core.time import parse_since, since_to_ts

    alias = resolve_workspace_alias(workspace)
    since_ts = since_to_ts(parse_since(since, field="--since"))

    source = build_source_service(actor="cli:slack-cursor-backfill")
    prior = source.cursor_get(_CONNECTOR)
    envelope = _load_cursors(prior) if prior is not None else _empty_envelope()
    state = envelope["workspaces"].get(alias)
    if state is None:
        state = _empty_state()

    if until is not None:
        until_ts = since_to_ts(parse_since(until, field="--until"))
    else:
        recorded = state["backfill"].get(channel_id)
        if recorded is not None:
            until_ts = recorded
        else:
            # Phase 23-F-2 (#536): pre-feature channel with no recorded
            # low-water. Default --until to the oldest already-ingested message
            # ts for this channel (the natural upper bound — history older than
            # it is the unfetched window). Only error when nothing is ingested.
            from opshub.cli._wiring import build_engine

            inferred = _oldest_observed_ts(
                build_engine, channel_id=channel_id, team_id=state["team_id"]
            )
            if inferred is None:
                raise ConfigError(
                    f"no recorded low-water mark for channel {channel_id!r} in "
                    f"workspace {alias!r} and no ingested messages to infer one; "
                    "pass --until explicitly (the floor the channel was last "
                    "synced from). See ADR-0038 §(f)."
                )
            until_ts = inferred
            import typer

            from opshub.cli._slack_status import (
                _ts_to_date,  # pyright: ignore[reportPrivateUsage]
            )

            typer.echo(
                f"notice: --until defaulted to the oldest ingested message ts for "
                f"{channel_id} ({_ts_to_date(inferred)}); pass --until to override.",
                err=True,
            )

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
        alias=alias,
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
    )
    source.cursor_set(_CONNECTOR, result.new_cursor, sync_started=False)
    return result.observed_count


def _oldest_observed_ts(
    engine_factory: Any, *, channel_id: str, team_id: str | None = None
) -> str | None:
    """Return the oldest ingested Slack message ts for ``channel_id``, or ``None``.

    Slack sources carry ``external_id = "{team_id}:{channel_id}:{ts}"`` (the
    message's natural key, re-keyed in Phase 24-B per [ADR-0041](
    ../../docs/adr/0041-slack-multi-workspace.md) §(a); see
    ``connectors/slack/mapper.py``), so the oldest ingested ts is the numeric
    ``min`` over the ts token of the channel's ``sources`` rows. When
    ``team_id`` is known (the workspace cursor entry is bound), the SQL
    pre-filter matches the exact ``"{team_id}:{channel_id}:"`` prefix so a
    channel id that collides across workspaces never pulls another
    workspace's history into the inference (Phase 24-C). When unbound, the
    pre-filter matches the channel id **between two colons** (the middle
    token) and the Python loop re-verifies the exact 3-token split. Legacy
    2-token rows (pre-Phase-24 ingest — only present when the operator
    skipped the ADR-0041 §(e) DB re-init) never match the pattern and are
    ignored. Used to default ``cursor backfill --until`` for a pre-feature
    channel (Phase 23-F-2, #536) **without reaching into the connector** —
    the connector stays decoupled from the projection; this CLI/query layer
    owns the lookup. Returns ``None`` when the channel has no ingested rows.
    """
    from sqlalchemy import select

    from opshub.projections.sources import sources_table

    pattern = f"{team_id}:{channel_id}:%" if team_id is not None else f"%:{channel_id}:%"
    engine = engine_factory()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(sources_table.c.external_id).where(
                    sources_table.c.connector_name == _CONNECTOR,
                    sources_table.c.external_id.like(pattern),
                )
            ).all()
    finally:
        engine.dispose()

    oldest: float | None = None
    oldest_raw: str | None = None
    for (external_id,) in rows:
        parts = str(external_id).split(":")
        if len(parts) != 3 or parts[1] != channel_id:
            # Either a malformed key or a LIKE false-positive (e.g. the
            # channel id substring-matching a different token). The exact
            # token check keeps the inference precise.
            continue
        if team_id is not None and parts[0] != team_id:
            continue
        suffix = parts[2]
        try:
            value = float(suffix)
        except ValueError:
            continue
        if oldest is None or value < oldest:
            oldest = value
            oldest_raw = suffix
    return oldest_raw
