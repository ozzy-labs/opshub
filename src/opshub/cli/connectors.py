"""``opshub connectors`` command (Phase 17-B, ADR-0031).

Replaces the legacy ``opshub connector list`` with the more natural
plural-noun-only form (``opshub connectors``). ADR-0031 §決定 (2)
covers the rationale.

Output is byte-identical to the legacy ``connector list`` surface:

* Empty registry → ``no connectors registered`` (stdout), exit 0.
* Populated registry → one connector name per line (stdout), exit 0.
"""

from __future__ import annotations

import typer

connectors_app = typer.Typer(
    name="connectors",
    help="List every registered SaaS connector (replaces `connector list`).",
    no_args_is_help=False,
    invoke_without_command=True,
)


@connectors_app.callback(invoke_without_command=True)
def connectors_list(ctx: typer.Context) -> None:
    """List every registered connector, one name per line.

    Empty registry prints ``no connectors registered`` and exits 0
    (a healthy "framework is wired, no connectors yet" report).
    """
    # If a subcommand was invoked, defer to it (we have none today,
    # but keep the harness clean for future ``opshub connectors
    # <verb>`` extensions).
    if ctx.invoked_subcommand is not None:
        return

    from opshub.connectors import discover_connectors

    connectors = discover_connectors()
    if not connectors:
        typer.echo("no connectors registered")
        return
    for connector in connectors:
        typer.echo(connector.name)
