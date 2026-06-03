"""``opshub google_workspace ...`` subcommands (Phase 17-B, ADR-0031).

Surface:

* ``opshub google_workspace sync`` — incremental sync from Google
  Drive ``changes.list`` (Phase 13).
* ``opshub google_workspace auth set`` — interactive OAuth paste-code
  flow. The refresh token is shared across Drive + Gmail + Calendar
  (single OAuth principal per Phase 14).
* ``opshub google_workspace auth test`` — verify the stored refresh
  token via Drive ``about.get``.
"""

from __future__ import annotations

import typer

google_workspace_app = typer.Typer(
    name="google_workspace",
    help="Google Workspace connector (Drive — auth shared with Gmail + Calendar).",
    no_args_is_help=True,
)

google_workspace_auth_app = typer.Typer(
    name="auth",
    help=(
        "Google OAuth (paste-code flow). The refresh token covers "
        "drive.readonly + gmail.readonly + calendar.readonly (Phase 14)."
    ),
    no_args_is_help=True,
)
google_workspace_app.add_typer(google_workspace_auth_app)


@google_workspace_app.command("sync")
def google_workspace_sync() -> None:
    """Incremental sync from Google Drive ``changes.list``.

    Uses the per-page ``startPageToken`` cursor stored in the
    ``connector_cursors`` projection. Workspace exports
    (Docs / Slides / Sheets) go through the markitdown pipeline when
    ``[connectors.google_workspace] content_extraction = true``.
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("google_workspace")


@google_workspace_auth_app.command("set")
def google_workspace_auth_set(
    token: str | None = typer.Option(
        None,
        "--token",
        help=(
            "Ignored on this connector: Google Workspace uses an interactive "
            "OAuth paste-code flow. Surfaced for usage symmetry with "
            "token-paste connectors; a warning is printed if supplied."
        ),
    ),
) -> None:
    """Run the Google OAuth paste-code flow (Phase 13 G3, scope-expanded in Phase 14).

    Opens the Google authorisation URL, waits for the operator to
    paste the redirected code, exchanges it for a refresh token, and
    writes the result to the OS keychain under
    ``connector:google_workspace:refresh_token``. The same refresh
    token is consumed by Drive (``opshub google_workspace sync``),
    Gmail (``opshub google_mail sync``), and Calendar
    (``opshub google_calendar sync``) — Phase 14 expanded the scope
    list so 1 OAuth principal serves all three connectors.

    The ``--token`` flag is not meaningful here — a warning is
    printed if supplied.
    """
    from opshub.cli._auth_common import run_oauth_paste_flow
    from opshub.cli._google_workspace_oauth import run_paste_code_flow

    run_oauth_paste_flow(
        label="google_workspace",
        runner=run_paste_code_flow,
        token_passed=token,
    )


@google_workspace_auth_app.command("test")
def google_workspace_auth_test() -> None:
    """Verify the stored Google refresh token via Drive ``about.get``.

    Requires ``[connectors.google_workspace] client_id`` AND
    ``client_secret`` in ``opshub.toml`` (Google's installed-app
    OAuth wire format demands the secret on every refresh round-trip
    even though Google documents it as non-secret). Exits 1 with an
    actionable ``ConfigError`` message if either is missing.
    """
    from opshub.cli._auth_common import run_auth_test
    from opshub.connectors.google_auth.auth import GoogleWorkspaceAuth
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError

    settings = OpsHubSettings()
    gws_cfg = settings.connectors.google_workspace
    if not gws_cfg.client_id:

        def _raise_no_client_id() -> dict[str, str]:
            raise ConfigError(
                "Google Workspace client_id is not configured. Set "
                "`[connectors.google_workspace] client_id` in "
                f"{settings.config_dir}/config.toml "
                "(run `opshub init` first if the file does not exist yet)."
            )

        run_auth_test(label="google_workspace", verifier=_raise_no_client_id)
        return

    if not gws_cfg.client_secret:

        def _raise_no_client_secret() -> dict[str, str]:
            raise ConfigError(
                "Google Workspace client_secret is not configured. Set "
                "`[connectors.google_workspace] client_secret` in "
                f"{settings.config_dir}/config.toml "
                "(run `opshub init` first if the file does not exist yet)."
            )

        run_auth_test(label="google_workspace", verifier=_raise_no_client_secret)
        return

    run_auth_test(
        label="google_workspace",
        verifier=GoogleWorkspaceAuth(
            client_id=gws_cfg.client_id,
            client_secret=gws_cfg.client_secret,
            redirect_uri=gws_cfg.redirect_uri,
        ).test_token,
    )
