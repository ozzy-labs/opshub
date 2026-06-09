"""``opshub slack ...`` subcommands (Phase 17-B, ADR-0031).

Surface:

* ``opshub slack sync`` — incremental sync from the Slack Web API
  (``[connectors.slack] channels``).
* ``opshub slack auth set [--token ...]`` — store a Slack OAuth token
  in the OS keychain (User Token preferred, Bot Token accepted —
  ADR-0018).
* ``opshub slack auth test`` — verify the stored token via
  ``auth.test``.
* ``opshub slack conversations`` — list conversations visible to the
  configured token (channels + DMs + MPIMs; #366 / #374).
* ``opshub slack mentions list`` — debug view of the
  ``slack_demand_digest`` projection (Phase 18-B, ADR-0033). Operator
  inspection only; the first-class skill surface is the Phase 18-C
  MCP ``slack.demand.list`` tool.

Module-level imports are restricted to ``__future__`` and ``typer``
so ``opshub --help`` cold start stays under the ~300ms budget set by
ADR-0001; heavy imports happen inside command callbacks (the
``test_cli_imports`` static check enforces this).
"""

from __future__ import annotations

import typer

slack_app = typer.Typer(
    name="slack",
    help="Slack connector (sync + auth + conversations discovery + demand digest).",
    no_args_is_help=True,
)

slack_auth_app = typer.Typer(
    name="auth",
    help="Slack OAuth token management.",
    no_args_is_help=True,
)
slack_app.add_typer(slack_auth_app)

slack_mentions_app = typer.Typer(
    name="mentions",
    help="Slack mention / DM demand digest (Phase 18-B, ADR-0033).",
    no_args_is_help=True,
)
slack_app.add_typer(slack_mentions_app)

slack_cursor_app = typer.Typer(
    name="cursor",
    help="Inspect / reset / backfill the Slack resume cursor (Phase 22-E, ADR-0038).",
    no_args_is_help=True,
)
slack_app.add_typer(slack_cursor_app)


@slack_app.command("sync")
def slack_sync(
    thread_activity_window: str | None = typer.Option(
        None,
        "--thread-activity-window",
        help=(
            "Late-reply polling activity window (Phase 20-C, "
            "ADR-0030 §(d)). Threads whose last reply is older "
            "than this window are skipped on the polling phase and "
            "pruned from the threads cursor. Accepts '7d' / '4w'; "
            "'all' (case-insensitive) disables the prune entirely "
            "(Phase 20-E, #478). Default 30d (from [connectors.slack] "
            "thread_activity_window in opshub.toml). Overrides the "
            "config-file value for this run only; persisted operator "
            "overrides belong in opshub.toml or "
            "OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW."
        ),
    ),
    no_backfill: bool = typer.Option(
        False,
        "--no-backfill",
        help=(
            "Suppress the automatic gap-backfill on floor lowering "
            "(Phase 22-D, ADR-0038). By default, lowering the date "
            "floor (sync_since / per-channel since) on a channel synced "
            "after the feature landed triggers a one-time backfill of "
            "the newly-uncovered window on the next sync. This flag "
            "disables that for this run only (the floor still bounds the "
            "forward fetch). Persisted override: [connectors.slack] "
            "backfill_on_floor_lower=false or "
            "OPSHUB_CONNECTORS__SLACK__BACKFILL_ON_FLOOR_LOWER."
        ),
    ),
) -> None:
    """Incremental sync from the Slack Web API.

    Uses the cursor stored in the ``connector_cursors`` projection.
    ``[connectors.slack] channels`` in ``opshub.toml`` (or
    ``OPSHUB_CONNECTORS__SLACK__CHANNELS``) selects the conversation
    set. ``[connectors.slack] sync_since`` (and per-channel ``since``)
    sets an optional date floor so messages older than the floor are
    never fetched, capping the cold-start backfill (Phase 20,
    :doc:`ADR-0036 </adr/0036-slack-sync-date-floor>`).
    ``[connectors.slack] thread_activity_window`` (and the
    ``--thread-activity-window`` flag) tunes the late-reply polling
    activity window (Phase 20-C, ADR-0030 §(d)): threads inactive
    longer than the window are skipped on the polling phase and
    pruned from the threads cursor. See
    :func:`opshub.cli._connector_common.run_connector_sync`
    for the shared driver invariants (cursor bracket, progress proxy,
    sanitised failure trail).
    """
    import os

    from opshub.cli._connector_common import run_connector_sync

    if thread_activity_window is not None:
        # The shared driver does not know about per-connector flags
        # (it only resolves a connector by name), so we surface the
        # override through the env-var path the pydantic-settings
        # nested delimiter already understands. Setting the env var
        # here is process-local — the operator's shell environment is
        # not mutated — so per-invocation overrides don't leak across
        # ``opshub`` calls. Persistent overrides live in
        # ``opshub.toml`` / the operator's exported env var.
        os.environ["OPSHUB_CONNECTORS__SLACK__THREAD_ACTIVITY_WINDOW"] = thread_activity_window

    if no_backfill:
        # Same process-local env-var shim as ``--thread-activity-window``
        # (Phase 22-D, ADR-0038). Only set on the truthy path so the
        # absence of the flag leaves the config-file / exported-env value
        # untouched (default ``True``).
        os.environ["OPSHUB_CONNECTORS__SLACK__BACKFILL_ON_FLOOR_LOWER"] = "false"

    # Phase 23-E (#535): surface a *visible* stderr notice when the sync
    # is a configured no-op (connector disabled, or no channels picked).
    # The connector's structured-log warning is invisible on a TTY, so a
    # sync that "succeeds" with 0 items looks like a working setup. The
    # notice is emitted before the run so it appears even when the
    # connector exits cleanly, and is suppressed by ``-q`` / ``--quiet``.
    is_noop = _emit_slack_sync_notice()

    run_connector_sync("slack")

    # On a properly-configured sync, close the setup loop with the
    # obvious follow-up (search / ask the assistant). Skipped on the
    # no-op path (the notice above already told the operator what to fix)
    # and under ``-q`` / ``--quiet``.
    if not is_noop and _notice_level_allows():
        typer.echo(
            "next: query the ingested messages with `opshub search <term>` "
            'or ask your assistant (e.g. "今日のまとめ").',
            err=True,
        )


def _notice_level_allows() -> bool:
    """Return whether INFO-tier stderr notices should print.

    ``-q`` / ``--quiet`` raises the root logger to WARNING (see
    :mod:`opshub.core.logging`); the Phase 23-E (#535) setup notices are
    INFO-tier guidance, so they stay silent once the operator asked for
    quiet output.
    """
    import logging

    return logging.getLogger().getEffectiveLevel() <= logging.INFO


def _emit_slack_sync_notice() -> bool:
    """Print a visible stderr notice when the Slack sync would be a no-op.

    Returns ``True`` when the sync is a *genuine* no-op (empty channels →
    the connector short-circuits to 0 items, see
    :meth:`SlackConnector.sync`), so the caller can skip the post-sync
    "next" hint. Returns ``False`` otherwise.

    Two configured states (issue #535 §problem 2) otherwise produce a
    quiet 0-item exit-0 that looks like success on a TTY, with only a
    structured log warning the operator never sees:

    * ``channels`` empty — the connector short-circuits to 0 items. This
      is the genuine no-op (``is_noop`` → ``True``).
    * ``[connectors.slack] enabled = false`` — the ``enabled`` flag is
      *informational* for the CLI (the driver runs the connector
      regardless; the flag is reserved for the future scheduler /
      autopilot, see the connector module docstring). So a disabled
      connector with channels still syncs — we surface a heads-up that
      the flag is off (a common "why is it still running?" surprise) but
      do **not** claim 0 items and do **not** mark it a no-op.

    Each notice is a plain-text ``notice:`` line naming the *reason* (not
    an exception type name) plus the concrete fix and a docs pointer,
    gated on the effective log level so ``-q`` / ``--quiet`` suppresses
    it. The empty-channels return value is independent of the log level,
    so the post-sync-hint suppression is not coupled to verbosity.
    """
    from opshub.core.config import OpsHubSettings

    slack = OpsHubSettings().connectors.slack
    quiet_ok = _notice_level_allows()

    if not slack.channels:
        if quiet_ok:
            typer.echo(
                "notice: [connectors.slack] channels is empty — sync will observe 0 "
                "items. Run `opshub slack conversations --format=toml` to discover "
                "ids and paste the block into opshub.toml. See docs/slack-setup.md.",
                err=True,
            )
        return True

    if not slack.enabled and quiet_ok:
        # Channels are configured, so the driver will actually sync (the
        # flag is informational, not a kill-switch). Surface the off-state
        # without claiming a no-op.
        typer.echo(
            "notice: [connectors.slack] enabled = false (the flag is "
            "informational — sync still runs). Set enabled = true in "
            "opshub.toml to mark the connector active. See docs/slack-setup.md.",
            err=True,
        )

    return False


@slack_auth_app.command("set")
def slack_auth_set(
    token: str | None = typer.Option(
        None,
        "--token",
        help="Token value. If omitted, read securely from stdin (hidden input).",
    ),
) -> None:
    """Store the Slack OAuth token in the OS keychain (ADR-0018).

    The token is stored under ``connector:slack:token`` (the same
    keyring slot the :class:`~opshub.connectors.slack.auth.SlackAuth`
    reader consults). User Token (``xoxp-``) is the first-class
    principal per ADR-0018; Bot Token (``xoxb-``) is accepted as an
    alternative for workspace-policy / audit-policy constraints.

    Override at runtime without touching the keychain with
    ``OPSHUB_CONNECTOR_SLACK_TOKEN`` (useful for CI / containers).
    """
    from opshub.cli._auth_common import set_token_credential
    from opshub.connectors.slack.auth import SLACK_TOKEN_SECRET_KEY

    set_token_credential(
        label="slack",
        keyring_key=SLACK_TOKEN_SECRET_KEY,
        token=token,
        next_action="opshub slack auth test  # verify the token + see granted scopes",
    )


@slack_auth_app.command("test")
def slack_auth_test() -> None:
    """Verify the stored Slack token via the ``auth.test`` Web API endpoint.

    Renders ``connector: slack`` + ``status: ok`` + the team / user /
    principal / scopes fields on success; exits 1 with ``status: failed``
    on :class:`~opshub.core.errors.ConfigError`. ``scopes`` lists the
    OAuth scopes Slack granted the token (from the ``x-oauth-scopes``
    response header, byte-symmetric with ``opshub github auth test``);
    ``(none)`` when Slack reports no scopes header.
    """
    from opshub.cli._auth_common import run_auth_test
    from opshub.connectors.slack.auth import SlackAuth

    run_auth_test(
        label="slack",
        verifier=SlackAuth().test_token,
        next_action=(
            "opshub slack conversations --format=toml  "
            "# discover channel ids → paste the block into opshub.toml"
        ),
    )


@slack_app.command("conversations")
def slack_conversations(
    output_format: str = typer.Option(
        "toml",
        "--format",
        help=(
            "Output format: table | toml | json. Default 'toml' "
            "(ADR-0035 §(a)): the primary use case is pasting the "
            "output into [connectors.slack] channels in opshub.toml. "
            "Pass --format=table to reproduce the pre-19-D default."
        ),
    ),
    filter_substring: str | None = typer.Option(
        None,
        "--filter",
        help=(
            "Case-insensitive substring match against the conversation "
            "name or participant display name."
        ),
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Maximum number of conversations to list (default: no limit).",
        min=1,
    ),
    types: str = typer.Option(
        "public,private,im,mpim",
        "--types",
        help=("Comma-separated conversation types: public, private, im, mpim (default: all four)."),
    ),
    include_archived: bool = typer.Option(
        False,
        "--include-archived",
        help="Include archived channels (default: excluded).",
    ),
    all_conversations: bool = typer.Option(
        False,
        "--all",
        help=(
            "Use conversations.list (workspace-wide) instead of "
            "users.conversations (joined-only). conversations.list does "
            "not return DM/MPIM rows. Incompatible with the engagement "
            "axis (--sort=last_self_post or --sort=name combined with "
            "--since), which only indexes self-member channels."
        ),
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help=(
            "Filter by recent activity. Accepts a relative duration "
            "(e.g. 7d, 2w) or an ISO date (e.g. 2026-05-01). The probe "
            "axis is selected by --sort (ADR-0035 §(c) §(d)): "
            "--sort=last_self_post (engagement axis, requires "
            "search:read on a User Token) runs one search.messages "
            "call ahead of the listing; --sort=last_activity (any-"
            "author axis) runs one conversations.history?limit=1 per "
            "row (requires *:history for the requested --types); "
            "--sort=name combined with --since takes the engagement "
            "axis as its implicit default. With --sort=last_self_post "
            "or --sort=last_activity but no --since, an implicit 90-"
            "day cutoff is applied (notice on stderr)."
        ),
    ),
    sort: str = typer.Option(
        "name",
        "--sort",
        help=(
            "Sort key (ADR-0035 §(c)): "
            "'name' (default) = display_name within type bucket; "
            "'last_self_post' = engagement-axis ts descending "
            "(requires search:read User Token); "
            "'last_activity' = any-author-axis ts descending "
            "(requires *:history per --types). With --sort=name and "
            "--since the engagement axis still runs as the implicit "
            "default for the ts filter / column."
        ),
    ),
) -> None:
    """List Slack conversations visible to the stored token (#366 / #374).

    Operators configure the Slack connector via
    ``[connectors.slack] channels = ["C012345...", ...]`` in
    ``opshub.toml``. Discovering those ids by hand (Slack Web UI →
    "Copy link") is painful in busy workspaces; this command surfaces
    every conversation the configured token participates in
    (channels + DMs + MPIMs) so the operator can paste the default
    ``--format=toml`` output straight into the config file.

    Exit codes:

    * ``0`` — conversations listed (zero matches is **not** an error).
    * ``1`` — config error (no token / SDK extras missing /
      ``--all`` + engagement-axis rejection) or runtime API failure
      (``invalid_auth`` / ``missing_scope`` on the listing or
      ``search.messages`` call / exhausted 429 retries / Bot Token on
      the engagement axis per ADR-0034 §(d) / ADR-0035 §(f)).
    * ``2`` — usage error (unknown ``--format`` / ``--types`` /
      ``--since`` / ``--sort`` value).

    See :mod:`opshub.cli._slack_conversations` for the renderer
    implementation and sort / output-format details (table / toml /
    json).
    """
    from opshub.cli._slack_conversations import (
        FORMAT_CHOICES,
        SORT_CHOICES,
        parse_since,
        parse_types,
        run_conversations_command,
    )
    from opshub.core.errors import ConfigError, ConnectorFailedError

    if output_format not in FORMAT_CHOICES:
        typer.echo(
            f"unknown --format value {output_format!r}; choose one of {', '.join(FORMAT_CHOICES)}",
            err=True,
        )
        raise typer.Exit(code=2)

    if sort not in SORT_CHOICES:
        # typer.BadParameter exits with code 2 — matches --format above.
        raise typer.BadParameter(
            f"unknown --sort value {sort!r}; choose one of {', '.join(SORT_CHOICES)}",
            param_hint="--sort",
        )

    # --all is incompatible with the engagement axis (ADR-0034 §(h),
    # ADR-0035 §(f) §組合せ拒否マトリクス). search.messages only
    # indexes messages the principal could see, so asking for
    # "workspace-wide channels where I posted" is logically the same
    # as the joined-only listing; silently trimming the result set
    # would hide the contradiction. The engagement axis fires on both
    # the explicit ``--sort=last_self_post`` path and the implicit
    # ``--sort=name`` + ``--since`` default (ADR-0035 §(d)); reject
    # both combinations. ``--sort=last_activity`` is workspace-wide
    # safe (per-row history call needs no self-membership).
    engagement_axis_requested = sort == "last_self_post" or (sort == "name" and since is not None)
    if all_conversations and engagement_axis_requested:
        typer.echo(
            "Error: --all is incompatible with engagement-axis sort "
            "(--sort=last_self_post or --sort=name + --since; "
            "search.messages indexes only self-member channels); "
            "use --sort=last_activity for workspace-wide activity.",
            err=True,
        )
        raise typer.Exit(code=1)

    parsed_types = parse_types(types)
    parsed_since = parse_since(since) if since is not None else None
    normalised_filter: str | None = filter_substring or None

    try:
        run_conversations_command(
            output_format=output_format,  # pyright: ignore[reportArgumentType]
            filter_substring=normalised_filter,
            limit=limit,
            types=parsed_types,
            include_archived=include_archived,
            all=all_conversations,
            since=parsed_since,
            sort=sort,  # pyright: ignore[reportArgumentType]
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ConnectorFailedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@slack_mentions_app.command("list")
def slack_mentions_list(
    output_format: str = typer.Option(
        "table",
        "--format",
        help="Output format: table | json | md.",
    ),
    types: str | None = typer.Option(
        None,
        "--types",
        help=(
            "Comma-separated channel types to include "
            "(``im,mpim,private,public``). Default: no filter."
        ),
    ),
    demand_kind: str | None = typer.Option(
        None,
        "--demand-kind",
        help=("Comma-separated demand kinds to include (``mention,dm``). Default: no filter."),
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Maximum number of digest rows to render (default: 50).",
        min=1,
    ),
) -> None:
    """Render the Slack demand digest projection (Phase 18-B, ADR-0033).

    Reads the ``slack_demand_digest`` table populated by the
    :class:`~opshub.projections.slack_demand_digest.SlackDemandDigestProjection`
    when ``opshub slack sync`` runs (or after a manual
    ``opshub projections rebuild``). The view is sorted by most
    recent demand first; ``--types`` and ``--demand-kind`` narrow the
    listing without changing the sort order.

    Exit codes:

    * ``0`` — rows rendered (zero matches is **not** an error).
    * ``1`` — config error (DB not initialised, schema mismatch).
    * ``2`` — usage error (unknown ``--format`` / ``--types`` /
      ``--demand-kind`` value).
    """
    # Lazy imports keep ``opshub --help`` under the ADR-0001 cold-start
    # budget and satisfy ``test_cli_imports`` (bans top-level
    # ``opshub.core`` / ``opshub.projections`` imports in this module).
    from opshub.cli._slack_mentions import (
        parse_demand_kinds,
        parse_types,
        render_mentions_list,
    )
    from opshub.cli._wiring import build_engine

    parsed_types = parse_types(types)
    parsed_demand_kinds = parse_demand_kinds(demand_kind)

    engine = build_engine()
    try:
        rendered = render_mentions_list(
            engine,
            fmt=output_format,
            types=parsed_types,
            demand_kinds=parsed_demand_kinds,
            limit=limit,
        )
    finally:
        engine.dispose()

    if rendered:
        typer.echo(rendered)


@slack_cursor_app.command("show")
def slack_cursor_show(
    output_format: str = typer.Option(
        "table",
        "--format",
        help="Output format: table | json. Default 'table'.",
    ),
) -> None:
    """Pretty-print the Slack compound resume cursor (channels / backfill / threads).

    Read-only view of the ``connector_cursors`` row for ``slack`` (Phase
    22-E, :doc:`ADR-0038 </adr/0038-slack-sync-gap-backfill>`). Exits 1 on a
    legacy / corrupt cursor (same ``ConfigError`` the sync path raises).
    """
    from opshub.cli._slack_cursor import parse_show_format, render_cursor_show
    from opshub.core.errors import ConfigError

    fmt = parse_show_format(output_format)
    try:
        typer.echo(render_cursor_show(output_format=fmt))
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@slack_cursor_app.command("reset")
def slack_cursor_reset(
    channels: str | None = typer.Option(
        None,
        "--channel",
        help="Channel id(s) to reset, comma-separated (e.g. C1,C2). Omit with --all.",
    ),
    reset_all: bool = typer.Option(
        False,
        "--all",
        help="Reset the cursor for every channel (full cold-start on next sync).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation (for non-interactive use).",
    ),
) -> None:
    """Drop cursor entries so the selected channels cold-start on the next sync.

    The **working** cursor reset (Phase 22-E, ADR-0038 §(f)) — unlike
    ``opshub projections rebuild``, which replays ``ConnectorSyncCompleted``
    and restores the same cursor. ⚠️ A reset channel re-fetches from its
    floor on the next sync, re-observing the previously-covered window
    (bounded inbox duplication until #522 lands); prefer ``opshub slack
    cursor backfill`` for a surgical, non-overlapping catch-up.

    Exit codes: 0 (reset / cancelled), 2 (neither --channel nor --all, or
    both).
    """
    from opshub.cli._slack_cursor import run_cursor_reset

    selected = [c.strip() for c in (channels or "").split(",") if c.strip()]
    if reset_all and selected:
        raise typer.BadParameter("--all is mutually exclusive with --channel.")
    if not reset_all and not selected:
        raise typer.BadParameter("specify --channel <id> (repeatable) or --all.")

    scope = "every channel" if reset_all else f"{len(selected)} channel(s): {', '.join(selected)}"
    if not yes:
        confirmed = typer.confirm(
            f"Reset the Slack cursor for {scope}? They will re-fetch from "
            "their floor on the next sync (may re-observe already-ingested "
            "messages).",
        )
        if not confirmed:
            typer.echo("aborted; cursor unchanged.")
            return

    removed, _ = run_cursor_reset(channels=selected, reset_all=reset_all)
    # ``--all`` hard-drops without parsing the prior cursor (so it can
    # recover a pre-Phase-20-B flat-dict, #531); ``run_cursor_reset``
    # returns -1 to signal "count unknown" on that path.
    if removed < 0:
        typer.echo("reset slack cursor: all channel entries cleared.")
    else:
        typer.echo(f"reset slack cursor: {removed} channel entr(y/ies) removed.")


@slack_cursor_app.command("backfill")
def slack_cursor_backfill(
    channel: str = typer.Option(
        ...,
        "--channel",
        help="Channel id to backfill.",
    ),
    since: str = typer.Option(
        ...,
        "--since",
        help="New (older) floor: relative '30d' / '4w' or ISO date '2026-01-01'.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help=(
            "Upper bound of the backfill window (exclusive of the forward "
            "set). Defaults to the channel's recorded low-water mark; "
            "required for a pre-feature channel with no recorded low-water "
            "(pass the floor it was last synced from)."
        ),
    ),
) -> None:
    """Fetch an explicit ``(since, until]`` window for one channel.

    The manual counterpart to the automatic gap-backfill (Phase 22-E,
    ADR-0038 §(f)) and the primary rescue for pre-feature channels: it
    fetches exactly the operator-specified window — disjoint from the
    already-covered region, so no inbox inflation — and advances the
    channel's low-water mark.

    Exit codes: 0 (backfilled), 1 (config error: no token / SDK extras
    missing / no recorded low-water without --until / since >= until /
    runtime API failure), 2 (bad --since / --until value).
    """
    from opshub.cli._slack_cursor import run_cursor_backfill
    from opshub.core.errors import ConfigError, ConnectorFailedError, ValidationError

    try:
        observed = run_cursor_backfill(channel_id=channel, since=since, until=until)
    except ValidationError as exc:
        # parse_since rejects a malformed --since / --until value.
        raise typer.BadParameter(str(exc)) from exc
    except (ConfigError, ConnectorFailedError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"backfilled slack channel {channel}: {observed} message(s) observed.")
