"""``opshub mcp ...`` subcommands (Phase 10 sub-issue C, ADR-0022).

Currently ships two commands:

* ``opshub mcp serve`` — start the stdio MCP server, blocking until
  the agent host disconnects. Agent hosts (Claude Code 等) spawn this
  as a subprocess; ADR-0022 §(a) requires stdio-only transport.
* ``opshub mcp tools`` — print the tool registry (name + read/write
  category + annotations) so an operator can inspect the surface
  without launching an actual agent. Useful for ``docs/mcp-setup.md``
  walkthroughs.

Module-level imports are restricted to ``__future__`` and ``typer`` so
``opshub --help`` cold start stays under the ~300ms budget (ADR-0001).
The static check in ``tests/integration/test_cli_imports.py`` enforces
this on every CI run; the heavy paths (MCP SDK, engine, registry) load
lazily inside the command callbacks.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

mcp_app = typer.Typer(
    name="mcp",
    help="Model Context Protocol server (Phase 10, ADR-0022).",
    no_args_is_help=True,
)


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Start the stdio MCP server for opshub.

    Blocks while serving the agent host over stdin / stdout. The
    server exits cleanly when the host closes the connection, so
    ``opshub mcp serve`` is suitable as a subprocess command in an
    agent host config (e.g. Claude Code ``mcpServers`` entry — see
    :file:`docs/mcp-setup.md`).

    Exit-code contract:

    * Clean shutdown (host disconnect) → exit 0.
    * Missing ``mcp`` extras (``ImportError`` on ``mcp.server.stdio``)
      → exit 2 + stderr install hint. Mirrors the other extras-gated
      commands (e.g. ``opshub recall`` with backend=disabled).
    * Unexpected exception during serve → re-raised; the top-level
      :func:`opshub.cli.app.main` handler maps :class:`OpsHubError`
      to a single ``Error: ...`` line.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001) and satisfy
    # the ``test_cli_imports`` static check.
    import asyncio

    try:
        from opshub.mcp.server import serve_stdio
    except ImportError as exc:
        typer.echo(
            "MCP extras missing. Install with: uv sync --extra mcp",
            err=True,
        )
        raise typer.Exit(code=2) from exc

    from opshub import __version__

    asyncio.run(serve_stdio(server_name="opshub", server_version=__version__))


@mcp_app.command("tools")
def mcp_tools(
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table | json.",
    ),
) -> None:
    """Print the MCP tool registry without starting the server.

    Useful for ``docs/mcp-setup.md`` walkthroughs and for operators
    auditing the read / write split. The output reflects the
    policy-as-data registry in :mod:`opshub.mcp._registry` — same
    source the running server uses (ADR-0022 §(c)).
    """
    import json

    from opshub.cli._wiring import build_engine
    from opshub.mcp._registry import ReadCategory
    from opshub.mcp.server import build_tool_specs_for_engine

    if fmt not in {"table", "json"}:
        typer.echo(f"unknown format {fmt!r}; expected table | json", err=True)
        raise typer.Exit(code=2)

    engine = build_engine()
    specs = build_tool_specs_for_engine(engine)

    if fmt == "json":
        payload = [
            {
                "name": s.name,
                "title": s.title,
                "category": s.category.value,
                "kind": "read" if isinstance(s.category, ReadCategory) else "write",
                "annotations": {
                    "readOnlyHint": s.policy.read_only,
                    "destructiveHint": s.policy.destructive,
                    "idempotentHint": s.policy.idempotent,
                    "openWorldHint": s.policy.open_world,
                },
                "description": s.description,
            }
            for s in specs
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    # Table format: ``<kind>  <name>  <title>``
    header_kind = "kind"
    header_name = "name"
    header_title = "title"
    name_width = max(len(header_name), max(len(s.name) for s in specs))
    typer.echo(f"{header_kind:<5}  {header_name:<{name_width}}  {header_title}")
    for s in specs:
        kind = "read" if isinstance(s.category, ReadCategory) else "write"
        typer.echo(f"{kind:<5}  {s.name:<{name_width}}  {s.title}")
