"""Shared infrastructure for per-noun ``opshub <connector> auth ...`` commands.

Phase 17-B (ADR-0031) splits the old ``opshub connector auth set/test``
dispatch tree into per-noun groups (``opshub <noun> auth set`` /
``opshub <noun> auth test``). The per-flavour wiring — token paste
prompt, OAuth paste-code dispatch, verifier resolution + ``status:
ok/failed`` rendering — is unchanged in semantics and lives here so
each noun's ``auth`` callbacks become thin shims.

Public surface:

* :func:`set_token_credential` — write a single bearer token to the
  keyring (used by Slack / GitHub / Teams / embedder / llm). Handles
  the ``--token`` / hidden-prompt fork and empty-string rejection.
* :func:`run_oauth_paste_flow` — invoke a connector-specific OAuth
  paste-code helper (MS365 / Box / Google Workspace). Surfaces the
  ``--token is ignored`` warning when the operator passed ``--token``
  by mistake.
* :func:`run_auth_test` — universal ``auth test`` driver. Takes a
  verifier callable + a display label and renders the
  ``connector: <label>`` / ``status: ok|failed`` / column-aligned
  key/value output.
* :class:`AuthTargetError` — base for usage / config errors that the
  caller maps to Typer exit codes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from collections.abc import Callable


# --------------------------------------------------------------------- #
# Token-paste flow (Slack / GitHub / Teams / embedder / llm)
# --------------------------------------------------------------------- #


def set_token_credential(
    *,
    label: str,
    keyring_key: str,
    token: str | None,
) -> None:
    """Write ``token`` (or prompted input) to the keyring under ``keyring_key``.

    Behaviour:

    * ``token is None`` → prompt with hidden input (matches the old
      ``opshub connector auth set`` UX).
    * Empty / whitespace-only token → exit 2 with ``token must be
      non-empty`` on stderr (silently storing an empty token would
      surface as a confusing 401 at sync time).
    * Otherwise → strip surrounding whitespace and write to the
      keyring; print ``stored token for connector '<label>'`` on
      stdout (byte-identical to the old surface).

    Lazy-imports :func:`opshub.core.secrets.set_secret` so the
    cold-start path is unaffected when the operator never touches
    auth.
    """
    from opshub.core.secrets import set_secret

    if token is None:
        raw: str = typer.prompt("Token", hide_input=True)
    else:
        raw = token

    stripped = raw.strip()
    if not stripped:
        typer.echo("token must be non-empty", err=True)
        raise typer.Exit(code=2)

    set_secret(keyring_key, stripped)
    typer.echo(f"stored token for connector {label!r}")


# --------------------------------------------------------------------- #
# OAuth paste-code flow (MS365 / Box / Google Workspace)
# --------------------------------------------------------------------- #


def run_oauth_paste_flow(
    *,
    label: str,
    runner: Callable[[], None],
    token_passed: str | None,
) -> None:
    """Drive an OAuth paste-code flow and surface ``--token is ignored`` warning.

    ``runner`` is a zero-arg callable from
    :mod:`opshub.cli._ms365_oauth` / :mod:`opshub.cli._box_oauth` /
    :mod:`opshub.cli._google_workspace_oauth` that handles the
    interactive consent / paste-code dance.

    The ``--token`` flag has no meaning on the OAuth path (the
    refresh token is produced by the code exchange, not pasted in).
    Rather than silently dropping it we surface an explicit warning
    on stderr so a misconfigured script makes the mistake visible.
    """
    if token_passed is not None:
        typer.echo(
            f"warning: --token is ignored for {label} (OAuth paste-code flow is used instead)",
            err=True,
        )
    runner()


# --------------------------------------------------------------------- #
# ``auth test`` universal driver
# --------------------------------------------------------------------- #


def run_auth_test(
    *,
    label: str,
    verifier: Callable[[], dict[str, str]],
) -> None:
    """Drive a connector's ``test_token`` verifier and render the result.

    ``verifier`` is the bound method / module function the connector
    exposes (see :mod:`opshub.connectors.<name>.auth`). The caller is
    responsible for performing any pre-flight ``ConfigError`` checks
    (e.g. resolving ``client_id`` from ``OpsHubSettings``) before
    handing the callable here — the resolution arms are too
    connector-specific to live in this shared helper.

    Output (byte-identical to the old ``opshub connector auth test``
    surface):

    * On success: ``connector: <label>`` + ``status:    ok`` +
      column-aligned ``<key>  <value>`` lines, all on stdout.
    * On :class:`ConfigError`: ``connector: <label>`` + ``status:
      failed`` + ``error:     <message>``, all on stderr; exit 1.

    Empty values render as ``(none)`` (operator readability beats
    strict round-trip fidelity here).
    """
    from opshub.core.errors import ConfigError

    try:
        result: dict[str, str] = verifier()
    except ConfigError as exc:
        typer.echo(f"connector: {label}", err=True)
        typer.echo("status:    failed", err=True)
        typer.echo(f"error:     {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Success path: print the connector header + status + result fields
    # in a stable column-aligned format. The order is determined by the
    # connector's ``test_token()`` return-dict order (Python dicts are
    # insertion-ordered) so each connector controls its own display.
    typer.echo(f"connector: {label}")
    typer.echo("status:    ok")
    # Find the widest key for column alignment. Cap at 20 chars so a
    # future overly-long key name doesn't blow out the layout.
    key_width = min(max((len(k) for k in result), default=0), 20)
    for k, v in result.items():
        display = v if v else "(none)"
        typer.echo(f"{k:<{key_width}}  {display}")
