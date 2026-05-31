"""Interactive OAuth paste-code flow for the Google Workspace connector.

Phase 13 G3 keeps the generic ``opshub connector auth set <name>``
surface (Phase 3 step A5) as the single entry point operators learn,
but the Google Workspace connector cannot use the plain "prompt for a
token string" branch — its credential is an OAuth refresh token that
only exists after a successful authorization-code exchange with
Google. We therefore intercept the ``google_workspace`` target in
:mod:`opshub.cli.connector` and dispatch to this helper, which:

1. Reads ``[connectors.google_workspace] client_id`` /
   ``client_secret`` / ``redirect_uri`` from the loaded
   :class:`opshub.core.config.OpsHubSettings`. Empty ``client_id`` /
   ``client_secret`` are rejected up front with a friendly hint
   pointing at the configuration path the operator must populate.
2. Constructs an
   :class:`opshub.connectors.google_workspace.auth.GoogleWorkspaceAuth`
   instance, prints the auth URL, and waits for the operator to paste
   the redirect URL (or bare code).
3. Calls :meth:`GoogleWorkspaceAuth.complete_auth_flow` which persists
   the refresh token via :mod:`opshub.core.secrets` (keyring +
   ``OPSHUB_CONNECTOR_GOOGLE_WORKSPACE_REFRESH_TOKEN`` env-var
   override per ADR-0014 §Phase 7 Validation, rotation pin リスト 3
   件目).

The helper lives behind a ``_`` prefix so the static cold-start guard
(``tests/integration/test_cli_imports``) does not require this file to
keep its module-level imports inside the whitelist — private helpers
are excluded from the parametrised test. The public
:mod:`opshub.cli.connector` module still defers its
``_google_workspace_oauth`` import inside the command callback to
preserve the ADR-0001 cold-start budget for operators who never touch
Google Workspace.

Heavy imports (``httpx`` via the auth module,
:mod:`opshub.core.config`, :mod:`opshub.core.secrets`) all happen
inside :func:`run_paste_code_flow` so that
``import opshub.cli._google_workspace_oauth`` itself stays cheap.

Symmetry with :mod:`opshub.cli._ms365_oauth` / :mod:`opshub.cli._box_oauth`
--------------------------------------------------------------------------

The three OAuth paste-code flow helpers are structurally identical by
design — the same error shape, the same prompt copy, the same
empty-paste / OAuth-failure exit codes (2 for misconfiguration, 1 for
network / OAuth failure). Any improvement to one (UX copy, retry
prompts, etc.) should ideally land in the other two at the same time.
The Phase 13 plan §3 G3 PR scope intentionally keeps this helper close
to the MS365 / Box shape so operators who have used either of those
recognise the flow immediately.
"""

from __future__ import annotations

import typer

__all__ = ["run_paste_code_flow"]


def run_paste_code_flow() -> None:
    """Drive the Google Workspace paste-code OAuth flow end-to-end.

    The function reads CLI input via :func:`typer.prompt` so the call
    site composes cleanly with the rest of ``opshub connector auth
    set ...``. Exit codes mirror Typer's convention:

    * ``0`` — refresh token persisted.
    * ``2`` — operator error (missing ``client_id`` / ``client_secret``
      / empty paste). We use ``2`` instead of ``1`` so
      misconfiguration is distinguishable from a network / OAuth
      error path (which would surface as the underlying
      :class:`ConfigError`).
    """
    # Lazy imports keep this module's import cost negligible — the
    # parent ``opshub.cli.connector`` already defers loading us until
    # the operator actually targets ``google_workspace``.
    from opshub.connectors.google_workspace.auth import GoogleWorkspaceAuth
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError

    settings = OpsHubSettings()
    cfg = settings.connectors.google_workspace
    if not cfg.client_id:
        typer.echo(
            "[connectors.google_workspace] client_id is not configured. "
            "Set it in opshub.toml (or via "
            "OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_ID) with the Google "
            "Cloud OAuth client (Installed Application type) ID, then re-run "
            "`opshub connector auth set google_workspace`.",
            err=True,
        )
        raise typer.Exit(code=2)
    if not cfg.client_secret:
        typer.echo(
            "[connectors.google_workspace] client_secret is not configured. "
            "Set it in opshub.toml (or via "
            "OPSHUB_CONNECTORS__GOOGLE_WORKSPACE__CLIENT_SECRET) with the "
            "Google Cloud OAuth client secret. Google documents this value as "
            "'not actually secret' for installed apps (it can be extracted "
            "from any distributed binary) but every OAuth round-trip still "
            "requires it on the wire.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        auth = GoogleWorkspaceAuth(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            redirect_uri=cfg.redirect_uri,
        )
    except ConfigError as exc:
        # Missing extras / invalid client_id reach the operator as a
        # single-line message; preserving the ConfigError chain via
        # ``from`` keeps the original cause visible under ``--tb``.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    auth_url = auth.start_auth_flow()
    typer.echo(
        "Open the following URL in a browser, sign in to your Google account, "
        "and approve the requested permissions (drive.readonly):"
    )
    typer.echo("")
    typer.echo(auth_url)
    typer.echo("")
    typer.echo(
        "After consenting Google will redirect to a "
        "`http://localhost/?code=...` URL (the page will fail to load — "
        "that is expected, opshub does not run a local web server). Paste "
        "either the full URL or just the `code` parameter below."
    )

    pasted: str = typer.prompt("Auth code / redirect URL")
    if not pasted.strip():
        typer.echo("paste was empty; aborting", err=True)
        raise typer.Exit(code=2)

    try:
        auth.complete_auth_flow(pasted)
    except ConfigError as exc:
        # The OAuth exchange failed (invalid code, expired code, scope
        # mismatch, etc). ``ConfigError`` (and its
        # :class:`GoogleAuthError` subclass) already carries an
        # actionable message so we propagate it verbatim rather than
        # reformatting.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("stored refresh token for connector 'google_workspace'")
