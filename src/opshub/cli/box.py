"""``opshub box ...`` subcommands (Phase 17-B, ADR-0031).

Surface:

* ``opshub box sync`` — incremental sync from the Box Events API
  (``stream_position`` cursor).
* ``opshub box auth set`` — interactive OAuth paste-code flow.
* ``opshub box auth test`` — verify the stored refresh token via
  Box ``users#me``.
"""

from __future__ import annotations

import typer

box_app = typer.Typer(
    name="box",
    help="Box connector (Events API stream_position cursor).",
    no_args_is_help=True,
)

box_auth_app = typer.Typer(
    name="auth",
    help="Box OAuth (paste-code flow).",
    no_args_is_help=True,
)
box_app.add_typer(box_auth_app)


@box_app.command("sync")
def box_sync() -> None:
    """Incremental sync from the Box Events API.

    Uses the ``stream_position`` cursor stored in the
    ``connector_cursors`` projection. Office documents observed during
    sync are content-extracted via the markitdown pipeline when
    ``[connectors.box] content_extraction = true``.
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("box")


@box_auth_app.command("set")
def box_auth_set(
    token: str | None = typer.Option(
        None,
        "--token",
        help=(
            "Ignored on this connector: Box uses an interactive OAuth "
            "paste-code flow. Surfaced for usage symmetry with token-paste "
            "connectors; a warning is printed if supplied."
        ),
    ),
) -> None:
    """Run the Box OAuth paste-code flow (Phase 7 step C1).

    Opens the Box authorisation URL, waits for the operator to paste
    the redirected code, exchanges it for a refresh token, and writes
    the result to the OS keychain. The ``--token`` flag is not
    meaningful here — a warning is printed if supplied.
    """
    from opshub.cli._auth_common import run_oauth_paste_flow
    from opshub.cli._box_oauth import run_paste_code_flow

    run_oauth_paste_flow(
        label="box",
        runner=run_paste_code_flow,
        token_passed=token,
    )


@box_auth_app.command("test")
def box_auth_test() -> None:
    """Verify the stored Box refresh token via Box ``users#me``.

    Requires ``[connectors.box] client_id`` configured in
    ``opshub.toml``. Exits 1 with an actionable ``ConfigError``
    message if the client id is missing.
    """
    from opshub.cli._auth_common import run_auth_test
    from opshub.connectors.box.auth import BoxAuth
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError

    settings = OpsHubSettings()
    box_client_id = settings.connectors.box.client_id
    if not box_client_id:

        def _raise_config_error() -> dict[str, str]:
            raise ConfigError(
                "Box client_id is not configured. Set "
                "`[connectors.box] client_id` in "
                f"{settings.config_dir}/config.toml "
                "(run `opshub init` first if the file does not exist yet)."
            )

        run_auth_test(label="box", verifier=_raise_config_error)
        return

    run_auth_test(
        label="box",
        verifier=BoxAuth(client_id=box_client_id).test_token,
    )
