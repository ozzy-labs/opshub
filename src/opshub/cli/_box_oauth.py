"""Interactive OAuth paste-code flow for the Box connector.

The Box connector cannot use the plain "prompt for a token string"
auth branch — its credential is an OAuth refresh token that only
exists after a successful authorization-code exchange with Box. The
per-noun ``opshub box auth set`` callback in :mod:`opshub.cli.box`
(Phase 17-B / ADR-0031 split, pre-Phase-17-B this lived in the
generic ``opshub connector auth set box`` dispatch inside
``opshub.cli.connector``) therefore dispatches to this helper, which:

1. Reads ``[connectors.box] client_id`` from the loaded
   :class:`opshub.core.config.OpsHubSettings`. The empty default for
   ``client_id`` is rejected up front with a friendly hint pointing at
   the configuration path the operator must populate.
2. Prompts for the **client secret** with ``hide_input=True`` and
   persists it via :mod:`opshub.core.secrets` under
   :data:`opshub.connectors.box.BOX_CLIENT_SECRET_SECRET_KEY`. Unlike
   the MS365 sibling (which uses an MSAL ``PublicClientApplication``
   that has no secret), Box's OAuth 2.0 flow requires the client
   secret on every exchange — we must store it alongside the refresh
   token.
3. Constructs an :class:`opshub.connectors.box.auth.BoxAuth` instance,
   prints the auth URL, and waits for the operator to paste the
   redirect URL (or bare code).
4. Calls :meth:`BoxAuth.complete_auth_flow` which persists the
   refresh token via :mod:`opshub.core.secrets` (keyring +
   ``OPSHUB_CONNECTOR_BOX_REFRESH_TOKEN`` env-var override) through
   boxsdk's ``store_tokens`` callback.

The helper lives behind a ``_`` prefix so the static cold-start guard
(``tests/integration/test_cli_imports``) does not require this file to
keep its module-level imports inside the whitelist — private helpers
are excluded from the parametrised test. The public
:mod:`opshub.cli.box` module still defers its ``_box_oauth``
import inside the command callback to preserve the ADR-0001
cold-start budget for operators who never touch Box.

Heavy imports (``boxsdk`` via the auth module, ``opshub.core.config``,
``opshub.core.secrets``) all happen inside :func:`run_paste_code_flow`
to keep ``import opshub.cli._box_oauth`` cheap.
"""

from __future__ import annotations

import typer

__all__ = ["run_paste_code_flow"]


def run_paste_code_flow() -> None:
    """Drive the Box paste-code OAuth flow end-to-end.

    The function reads CLI input via :func:`typer.prompt` so the call
    site composes cleanly with the rest of ``opshub connector auth
    set ...``. Exit codes mirror Typer's convention:

    * ``0`` — refresh token persisted.
    * ``2`` — operator error (missing ``client_id``, empty paste,
      empty client_secret). We use ``2`` instead of ``1`` so
      misconfiguration is distinguishable from a network / SDK error
      path (which would surface as the underlying :class:`ConfigError`).
    """
    # Lazy imports keep this module's import cost negligible — the
    # parent ``opshub.cli.box`` already defers loading us until
    # the operator actually invokes ``opshub box auth set``.
    from opshub.connectors.box import BOX_CLIENT_SECRET_SECRET_KEY, BoxAuth
    from opshub.core.config import OpsHubSettings
    from opshub.core.secrets import set_secret

    settings = OpsHubSettings()
    cfg = settings.connectors.box
    if not cfg.client_id:
        typer.echo(
            "[connectors.box] client_id is not configured. "
            "Set it in opshub.toml (or via "
            "OPSHUB_CONNECTORS__BOX__CLIENT_ID) with your Box developer "
            "app's client id, then re-run "
            "`opshub box auth set`.",
            err=True,
        )
        raise typer.Exit(code=2)

    # ``hide_input=True`` keeps the secret off the terminal — Box's
    # client_secret is as sensitive as the refresh token itself, so
    # the same prompt hygiene applies.
    raw_secret: str = typer.prompt("Box client_secret", hide_input=True)
    client_secret = raw_secret.strip()
    if not client_secret:
        typer.echo("client_secret must be non-empty", err=True)
        raise typer.Exit(code=2)

    # Persist the secret before driving the OAuth round trip so a
    # subsequent ``BoxAuth()`` invocation in another process can
    # resolve it through the keyring. We still pass it explicitly
    # below so this CLI invocation does not depend on the keyring
    # round-trip succeeding (testing seam + protection against a
    # mis-configured keyring backend that silently swallows writes).
    set_secret(BOX_CLIENT_SECRET_SECRET_KEY, client_secret)

    auth = BoxAuth(client_id=cfg.client_id, client_secret=client_secret)
    auth_url = auth.start_auth_flow()

    typer.echo(
        "\n1. Open this URL in a browser and authorise your Box app:\n"
        f"\n   {auth_url}\n"
        "\n2. After Box redirects, copy the full redirect URL (or just the "
        "'code' query parameter) and paste it below.\n"
    )
    # ``hide_input=True`` so the code does not linger in terminal
    # scrollback (it is short-lived but still grants exchange rights).
    pasted: str = typer.prompt("Paste redirect URL or code", hide_input=True)
    if not pasted.strip():
        typer.echo("paste must be non-empty", err=True)
        raise typer.Exit(code=2)
    auth.complete_auth_flow(pasted)
    typer.echo("Box OAuth flow complete; refresh token stored in keyring.")
