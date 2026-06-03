"""Interactive OAuth paste-code flow for the Microsoft 365 connector.

The Microsoft 365 connector cannot use the plain "prompt for a token
string" auth branch — its credential is an OAuth refresh token that
only exists after a successful authorization-code exchange with
Microsoft. The per-noun ``opshub ms365 auth set`` callback in
:mod:`opshub.cli.ms365` (Phase 17-B / ADR-0031 split, pre-Phase-17-B
this lived in the generic ``opshub connector auth set ms365`` dispatch
inside ``opshub.cli.connector``) therefore dispatches to this helper,
which:

1. Reads ``[connectors.ms365] client_id`` / ``authority`` from the
   loaded :class:`opshub.core.config.OpsHubSettings`. The empty default
   for ``client_id`` is rejected up front with a friendly hint pointing
   at the configuration path the operator must populate.
2. Constructs an :class:`opshub.connectors.ms365.auth.MS365Auth`
   instance, prints the auth URL, and waits for the operator to paste
   the redirect URL (or bare code).
3. Calls :meth:`MS365Auth.complete_auth_flow` which persists the
   refresh token via :mod:`opshub.core.secrets` (keyring +
   ``OPSHUB_CONNECTOR_MS365_REFRESH_TOKEN`` env-var override).

The helper lives behind a ``_`` prefix so the static cold-start guard
(``tests/integration/test_cli_imports``) does not require this file to
keep its module-level imports inside the whitelist — private helpers
are excluded from the parametrised test. The public
:mod:`opshub.cli.ms365` module still defers its ``_ms365_oauth``
import inside the command callback to preserve the ADR-0001
cold-start budget for operators who never touch MS365.

Heavy imports (``msal`` via the auth module, ``opshub.core.config``,
``opshub.core.secrets``) all happen inside :func:`run_paste_code_flow`
to keep ``import opshub.cli._ms365_oauth`` cheap.
"""

from __future__ import annotations

import typer

__all__ = ["run_paste_code_flow"]


def run_paste_code_flow() -> None:
    """Drive the Microsoft 365 paste-code OAuth flow end-to-end.

    The function reads CLI input via :func:`typer.prompt` so the call
    site composes cleanly with the rest of ``opshub connector auth
    set ...``. Exit codes mirror Typer's convention:

    * ``0`` — refresh token persisted.
    * ``2`` — operator error (missing ``client_id`` / empty paste).
      We use ``2`` instead of ``1`` so misconfiguration is
      distinguishable from a network / SDK error path (which would
      surface as the underlying :class:`ConfigError`).
    """
    # Lazy imports keep this module's import cost negligible — the
    # parent ``opshub.cli.ms365`` already defers loading us until
    # the operator actually invokes ``opshub ms365 auth set``.
    from opshub.connectors.ms365.auth import MS365Auth
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError

    settings = OpsHubSettings()
    cfg = settings.connectors.ms365
    if not cfg.client_id:
        # Operator-actionable error: pyproject installs ``msal`` but
        # cannot register the Azure AD app for them; the
        # ``client_id`` must be supplied via opshub.toml or the
        # documented env var override.
        typer.echo(
            "[connectors.ms365] client_id is not configured. "
            "Set it in opshub.toml (or via "
            "OPSHUB_CONNECTORS__MS365__CLIENT_ID) with the Azure AD "
            "app's application (client) ID, then re-run "
            "`opshub ms365 auth set`.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        auth = MS365Auth(client_id=cfg.client_id, authority=cfg.authority)
    except ConfigError as exc:
        # Missing extras / invalid client_id reach the operator as a
        # single-line message; preserving the ConfigError chain via
        # ``from`` keeps the original cause visible under ``--tb``.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    auth_url = auth.start_auth_flow()
    typer.echo(
        "Open the following URL in a browser, sign in to your Microsoft "
        "account, and approve the requested permissions:"
    )
    typer.echo("")
    typer.echo(auth_url)
    typer.echo("")
    typer.echo(
        "After consenting Microsoft will redirect to a "
        "`...nativeclient?code=...` URL. Paste either the full URL or "
        "just the `code` parameter below."
    )

    pasted: str = typer.prompt("Auth code / redirect URL")
    if not pasted.strip():
        typer.echo("paste was empty; aborting", err=True)
        raise typer.Exit(code=2)

    try:
        auth.complete_auth_flow(pasted)
    except ConfigError as exc:
        # The OAuth exchange failed (invalid code, expired code, etc).
        # ``ConfigError`` already carries an actionable message so we
        # propagate it verbatim rather than reformatting.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("stored refresh token for connector 'connector:ms365'")
