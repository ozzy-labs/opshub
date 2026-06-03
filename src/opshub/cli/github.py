"""``opshub github ...`` subcommands (Phase 17-B, ADR-0031).

Surface:

* ``opshub github sync`` — incremental sync from the GitHub REST API
  (``OPSHUB_CONNECTOR_GITHUB_REPO=owner/repo``).
* ``opshub github auth set [--token ...]`` — store a GitHub PAT in
  the OS keychain.
* ``opshub github auth test`` — verify the stored PAT via
  ``GET /user``.

ADR-0001 lazy-import rule: heavy imports live inside the callbacks.
"""

from __future__ import annotations

import typer

github_app = typer.Typer(
    name="github",
    help="GitHub connector (sync + auth).",
    no_args_is_help=True,
)

github_auth_app = typer.Typer(
    name="auth",
    help="GitHub PAT management.",
    no_args_is_help=True,
)
github_app.add_typer(github_auth_app)


@github_app.command("sync")
def github_sync() -> None:
    """Incremental sync from the GitHub REST API.

    Uses the cursor stored in the ``connector_cursors`` projection.
    Repo is selected by ``OPSHUB_CONNECTOR_GITHUB_REPO=owner/repo`` (or
    ``[connectors.github] repo`` in ``opshub.toml``).
    """
    from opshub.cli._connector_common import run_connector_sync

    run_connector_sync("github")


@github_auth_app.command("set")
def github_auth_set(
    token: str | None = typer.Option(
        None,
        "--token",
        help="Token value. If omitted, read securely from stdin (hidden input).",
    ),
) -> None:
    """Store a GitHub Personal Access Token in the OS keychain (ADR-0014).

    The token is stored under the keyring slot the
    :mod:`opshub.connectors.github.auth` reader consults. Override at
    runtime without touching the keychain with
    ``OPSHUB_CONNECTOR_GITHUB_PAT`` (useful for CI / containers).
    """
    from opshub.cli._auth_common import set_token_credential
    from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY

    set_token_credential(
        label="github",
        keyring_key=GITHUB_PAT_SECRET_KEY,
        token=token,
    )


@github_auth_app.command("test")
def github_auth_test() -> None:
    """Verify the stored GitHub PAT via ``GET /user``.

    Renders ``connector: github`` + ``status: ok`` + the
    ``login`` / ``name`` / ``scopes`` fields on success; exits 1 with
    ``status: failed`` on :class:`~opshub.core.errors.ConfigError`.
    """
    from opshub.cli._auth_common import run_auth_test
    from opshub.connectors.github.auth import test_token as github_test_token

    run_auth_test(label="github", verifier=github_test_token)
