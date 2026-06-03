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

Module-level imports are restricted to ``__future__`` and ``typer``
so ``opshub --help`` cold start stays under the ~300ms budget set by
ADR-0001; heavy imports happen inside command callbacks (the
``test_cli_imports`` static check enforces this).
"""

from __future__ import annotations

import typer

slack_app = typer.Typer(
    name="slack",
    help="Slack connector (sync + auth + conversations discovery).",
    no_args_is_help=True,
)

slack_auth_app = typer.Typer(
    name="auth",
    help="Slack OAuth token management.",
    no_args_is_help=True,
)
slack_app.add_typer(slack_auth_app)


@slack_app.command("sync")
def slack_sync() -> None:
    """Incremental sync from the Slack Web API.

    Uses the cursor stored in the ``connector_cursors`` projection.
    ``[connectors.slack] channels`` in ``opshub.toml`` (or
    ``OPSHUB_CONNECTORS_SLACK_CHANNELS``) selects the conversation
    set. See :func:`opshub.cli._connector_common.run_connector_sync`
    for the shared driver invariants (cursor bracket, progress proxy,
    sanitised failure trail).
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("slack")


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
    )


@slack_auth_app.command("test")
def slack_auth_test() -> None:
    """Verify the stored Slack token via the ``auth.test`` Web API endpoint.

    Renders ``connector: slack`` + ``status: ok`` + the team / user /
    principal fields on success; exits 1 with ``status: failed`` on
    :class:`~opshub.core.errors.ConfigError`.
    """
    from opshub.cli._auth_common import run_auth_test
    from opshub.connectors.slack.auth import SlackAuth

    run_auth_test(label="slack", verifier=SlackAuth().test_token)


@slack_app.command("conversations")
def slack_conversations(
    output_format: str = typer.Option(
        "table",
        "--format",
        help="Output format: table | toml | json.",
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
            "not return DM/MPIM rows."
        ),
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help=(
            "Filter by last-message timestamp. Accepts a relative "
            "duration (e.g. 7d, 2w) or an ISO date (e.g. 2026-05-01). "
            "Triggers one conversations.history call per conversation; "
            "requires *:history scopes for the requested --types."
        ),
    ),
) -> None:
    """List Slack conversations visible to the stored token (#366 / #374).

    Operators configure the Slack connector via
    ``[connectors.slack] channels = ["C012345...", ...]`` in
    ``opshub.toml``. Discovering those ids by hand (Slack Web UI →
    "Copy link") is painful in busy workspaces; this command surfaces
    every conversation the configured token participates in
    (channels + DMs + MPIMs) so the operator can paste the
    ``--format toml`` output straight into the config file.

    Exit codes:

    * ``0`` — conversations listed (zero matches is **not** an error).
    * ``1`` — config error (no token / SDK extras missing) or runtime
      API failure (``invalid_auth`` / ``missing_scope`` on the listing
      call / exhausted 429 retries).
    * ``2`` — usage error (unknown ``--format`` / ``--types`` /
      ``--since`` value).

    See :mod:`opshub.cli._slack_conversations` for the renderer
    implementation and sort / output-format details (table / toml /
    json).
    """
    from opshub.cli._slack_conversations import (
        FORMAT_CHOICES,
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
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ConnectorFailedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
