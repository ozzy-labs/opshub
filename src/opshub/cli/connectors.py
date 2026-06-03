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

    # Connector packages register themselves with the process-wide
    # registry as an import-side-effect (``register_connector(...)``
    # at module top). The Phase 17-B refactor (PR #414) split the old
    # ``opshub connector list`` into this new surface but forgot to
    # carry over the import call, so on a fresh process the registry
    # is empty and the command misleadingly reported "no connectors
    # registered" even though every connector was wired in-tree.
    # ``import_connector_modules`` is the shared helper that both
    # this CLI surface and ``opshub.mcp._writes::connector.sync`` now
    # call (see ``opshub.connectors._discovery``).
    from opshub.connectors._discovery import import_connector_modules

    import_connector_modules()

    from opshub.connectors import discover_connectors

    connectors = discover_connectors()
    if not connectors:
        typer.echo("no connectors registered")
        return
    for connector in connectors:
        typer.echo(connector.name)
