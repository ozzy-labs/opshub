"""``opshub teams ...`` subcommands (Phase 17-B, ADR-0031).

Surface:

* ``opshub teams sync`` — incremental sync from Microsoft Graph chat
  delta endpoint (Phase 11 F5).
* ``opshub teams auth set [--token ...]`` — store a Microsoft Graph
  User Token in the OS keychain.
* ``opshub teams auth test`` — placeholder; Teams currently lacks a
  dedicated verifier in :mod:`opshub.connectors.teams.auth` so this
  command surfaces a friendly stub error pointing operators at
  ``opshub teams sync`` for end-to-end verification.

Phase 17-B BREAKING CHANGE: the old ``opshub connector auth test
teams`` path was not implemented in the legacy CLI either — its
``_resolve_auth_test_verifier`` dispatch (since refactored into
:func:`opshub.cli._auth_common.run_auth_test` per ADR-0031) never
had a teams arm. Per-noun parity here is therefore achieved by the
same "unsupported verifier" stub — no behaviour drift.
"""

from __future__ import annotations

import typer

teams_app = typer.Typer(
    name="teams",
    help="Microsoft Teams connector (Graph chat delta).",
    no_args_is_help=True,
)

teams_auth_app = typer.Typer(
    name="auth",
    help="Microsoft Teams Graph User Token management.",
    no_args_is_help=True,
)
teams_app.add_typer(teams_auth_app)


@teams_app.command("sync")
def teams_sync() -> None:
    """Incremental sync from Microsoft Graph chat delta endpoint.

    Uses the per-chat delta cursor stored in the
    ``connector_cursors`` projection. Invalidated tokens fall back to
    a full-pass refresh.
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("teams")


@teams_auth_app.command("set")
def teams_auth_set(
    token: str | None = typer.Option(
        None,
        "--token",
        help="Token value. If omitted, read securely from stdin (hidden input).",
    ),
) -> None:
    """Store a Microsoft Graph User Token for Teams in the OS keychain.

    Unlike MS365 / Box this connector accepts a pre-resolved token
    directly rather than running an in-process OAuth dance —
    operators acquire the token via Azure Portal / MSAL device code
    flow and paste the result into ``--token`` (or stdin). See
    ``docs/teams-setup.md`` for the end-to-end procedure.

    The token is stored under ``connector:teams:token`` so the CLI
    writer + TeamsAuth reader cannot drift (mirrors the
    Slack / MS365 / Box precedent).
    """
    from opshub.cli._auth_common import set_token_credential
    from opshub.connectors.teams.auth import TEAMS_TOKEN_SECRET_KEY

    set_token_credential(
        label="teams",
        keyring_key=TEAMS_TOKEN_SECRET_KEY,
        token=token,
    )


@teams_auth_app.command("test")
def teams_auth_test() -> None:
    """Verify the stored Teams Graph User Token.

    Currently a stub: :mod:`opshub.connectors.teams.auth` does not
    expose a standalone ``test_token`` callable (the original
    ``connector auth test`` dispatch had no teams arm either —
    verification was end-to-end via ``opshub teams sync``). This
    command surfaces a friendly ``ConfigError`` pointing operators at
    that sync path; future work can add a dedicated ``GET /me`` probe
    via the same shared driver.
    """
    from opshub.cli._auth_common import run_auth_test
    from opshub.core.errors import ConfigError

    def _raise_unsupported() -> dict[str, str]:
        raise ConfigError(
            "Teams does not yet expose a standalone `auth test` verifier. "
            "Run `opshub teams sync` to perform an end-to-end check "
            "(the connector will surface invalid_auth / missing_scope "
            "via the standard ConnectorSyncFailed event)."
        )

    run_auth_test(label="teams", verifier=_raise_unsupported)
