"""``opshub ms365 ...`` subcommands (Phase 17-B, ADR-0031).

Surface:

* ``opshub ms365 sync`` — incremental sync from Microsoft Graph
  (Calendar + OneDrive + Outlook delta endpoints).
* ``opshub ms365 auth set`` — interactive OAuth paste-code flow
  (refresh token stored in OS keychain).
* ``opshub ms365 auth test`` — verify the stored refresh token via
  ``GET /me``.
"""

from __future__ import annotations

import typer

ms365_app = typer.Typer(
    name="ms365",
    help="Microsoft 365 connector (Calendar / OneDrive / Outlook).",
    no_args_is_help=True,
)

ms365_auth_app = typer.Typer(
    name="auth",
    help="Microsoft 365 OAuth (paste-code flow).",
    no_args_is_help=True,
)
ms365_app.add_typer(ms365_auth_app)


@ms365_app.command("sync")
def ms365_sync() -> None:
    """Incremental sync from Microsoft Graph (Calendar + OneDrive + Outlook).

    Uses the per-endpoint delta cursor stored in the
    ``connector_cursors`` projection. Outlook bodies are retained in
    full (head-truncated at 500K chars) per Phase 11 F4-c.
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("ms365")


@ms365_auth_app.command("set")
def ms365_auth_set(
    token: str | None = typer.Option(
        None,
        "--token",
        help=(
            "Ignored on this connector: MS365 uses an interactive OAuth "
            "paste-code flow. Surfaced for usage symmetry with token-paste "
            "connectors; a warning is printed if supplied."
        ),
    ),
) -> None:
    """Run the MS365 OAuth paste-code flow (Phase 7 step B1).

    Opens the MSAL authorisation URL, waits for the operator to paste
    the redirected code, exchanges it for a refresh token, and writes
    the result to the OS keychain. The ``--token`` flag is not
    meaningful here — a warning is printed if supplied.
    """
    from opshub.cli._auth_common import run_oauth_paste_flow
    from opshub.cli._ms365_oauth import run_paste_code_flow

    run_oauth_paste_flow(
        label="ms365",
        runner=run_paste_code_flow,
        token_passed=token,
    )


@ms365_auth_app.command("test")
def ms365_auth_test() -> None:
    """Verify the stored MS365 refresh token via Microsoft Graph ``GET /me``.

    Requires ``[connectors.ms365] client_id`` configured in
    ``opshub.toml`` (run ``opshub init`` first if the file does not
    exist yet). Exits 1 with an actionable ``ConfigError`` message if
    the client id is missing.
    """
    from opshub.cli._auth_common import run_auth_test
    from opshub.connectors.ms365.auth import MS365Auth
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError

    settings = OpsHubSettings()
    ms365_client_id = settings.connectors.ms365.client_id
    if not ms365_client_id:
        # Surface ``ConfigError`` via the shared driver so the
        # ``connector: ms365`` / ``status: failed`` / ``error: ...``
        # framing is byte-identical to the runtime API failure path.
        def _raise_config_error() -> dict[str, str]:
            raise ConfigError(
                "MS365 client_id is not configured. Set "
                "`[connectors.ms365] client_id` in "
                f"{settings.config_dir}/config.toml "
                "(run `opshub init` first if the file does not exist yet)."
            )

        run_auth_test(label="ms365", verifier=_raise_config_error)
        return

    run_auth_test(
        label="ms365",
        verifier=MS365Auth(client_id=ms365_client_id).test_token,
    )
