"""``opshub graph`` — graph traversal queries (Phase 8 step D1, ADR-0017).

Read-only queries over the ``links`` projection materialised by
Phase 8 A2 + B2 + C1. Three subcommands:

* ``opshub graph related <entity> [--direction outgoing|incoming|both]
  [--type <type>] [--limit N] [--format md|json|dot]``
* ``opshub graph trace <entity> [--depth N] [--type <type>]
  [--format md|json|dot]``
* ``opshub graph expand <entity> [--depth N] [--type <type>]
  [--format md|json|dot]`` (depends on C2 — see note below)

Entities use the same ``<entity_type>:<entity_id>`` syntax as
``opshub link``. ``--format dot`` produces Graphviz DOT output which
can be piped into ``dot -Tpng`` / ``dot -Tsvg`` for rendering; the
escaper handles quote / backslash characters in entity ids so
arbitrary ULIDs / connector ids stay quoteable.

``graph expand`` is fully wired in Phase 8 step D2: it calls
:meth:`LinkService.expand` to materialise a bidirectional N-hop
:class:`~opshub.services.links.GraphSubset` rooted at the supplied
entity and renders it via :func:`opshub.cli._render.render_graph_subset_md`
/ :func:`render_graph_subset_json` / :func:`render_graph_subset_dot`.
The depth ceiling pinned by ADR-0017 §決定 (e) (max 5) is enforced
inside the service and surfaces as :class:`ConfigError` here.

Cold-start guard
----------------

Module-level imports stay within the M6 cold-start whitelist
(``__future__`` / ``typer`` / ``typing``). Every ``opshub`` import
lives inside the command function body so the cold-start budget is
paid only when the operator actually invokes a graph subcommand
(ADR-0001).

Exit-code contract
------------------

* ``0`` — success.
* ``1`` — :class:`~opshub.core.errors.OpsHubError` raised by the
  service.
* ``2`` — :class:`~opshub.core.errors.ConfigError` (depth ceiling
  exceeded, depth negative, or DB not initialised) or
  :class:`typer.BadParameter` (malformed entity argument / unknown
  direction / unknown format).
"""

from __future__ import annotations

from typing import Annotated

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

graph_app = typer.Typer(
    name="graph",
    help="Graph traversal queries (related / trace / expand).",
    no_args_is_help=True,
)


_ALLOWED_DIRECTIONS = ("outgoing", "incoming", "both")
_ALLOWED_FORMATS = ("md", "json", "dot")


def _parse_entity_arg(value: str, *, param_name: str) -> tuple[str, str]:
    """Parse ``<entity_type>:<entity_id>`` into a tuple.

    Shares the same shape as the helper in :mod:`opshub.cli.link` but
    is duplicated here so each module's M6 cold-start audit only has
    to inspect its own imports (the link module is not always loaded
    when ``opshub graph ...`` runs).
    """
    if ":" not in value:
        raise typer.BadParameter(
            f"expected '<entity_type>:<entity_id>' (e.g. 'task:01J...'), got {value!r}",
            param_hint=param_name,
        )
    entity_type, _, entity_id = value.partition(":")
    if not entity_type or not entity_id:
        raise typer.BadParameter(
            f"expected non-empty type and id in '<entity_type>:<entity_id>', got {value!r}",
            param_hint=param_name,
        )
    return entity_type, entity_id


def _validate_format(value: str) -> None:
    """Reject formats outside the allowed set with a clean usage error."""
    if value not in _ALLOWED_FORMATS:
        typer.echo(
            f"Error: invalid --format {value!r}; expected one of {', '.join(_ALLOWED_FORMATS)}",
            err=True,
        )
        raise typer.Exit(code=2)


def _validate_direction(value: str) -> None:
    """Reject directions outside the allowed set with a clean usage error."""
    if value not in _ALLOWED_DIRECTIONS:
        typer.echo(
            f"Error: invalid --direction {value!r}; expected one of "
            f"{', '.join(_ALLOWED_DIRECTIONS)}",
            err=True,
        )
        raise typer.Exit(code=2)


@graph_app.command("related")
def graph_related(
    entity_arg: Annotated[
        str,
        typer.Argument(
            metavar="ENTITY",
            help="entity to query, formatted '<entity_type>:<entity_id>'.",
        ),
    ],
    direction: Annotated[
        str,
        typer.Option(
            "--direction",
            help="outgoing | incoming | both (default 'both').",
        ),
    ] = "both",
    type_filter: Annotated[
        list[str] | None,
        typer.Option(
            "--type",
            "-t",
            help="Filter by link_type (repeatable).",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Maximum number of related links to return (default 50).",
        ),
    ] = 50,
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: md | json | dot. Defaults to md.",
        ),
    ] = "md",
) -> None:
    """List 1-hop neighbours of ``ENTITY``."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from typing import Literal, cast

    from opshub.cli._render import (
        render_link_list_json,
        render_link_list_md,
        render_links_dot,
    )
    from opshub.cli._wiring import build_link_service
    from opshub.core.errors import ConfigError, OpsHubError

    _validate_format(output_format)
    _validate_direction(direction)
    entity_type, entity_id = _parse_entity_arg(entity_arg, param_name="ENTITY")

    service = build_link_service(actor="cli:graph_related")
    # Narrow ``direction`` to the LinkService ``_Direction`` literal
    # so static checkers do not flag the cross-module call (the
    # runtime check above already enforces the value space).
    direction_literal = cast(
        "Literal['outgoing', 'incoming', 'both']",
        direction,
    )
    try:
        links = service.related(
            entity_type,
            entity_id,
            direction=direction_literal,
            link_types=type_filter,
            limit=limit,
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if output_format == "json":
        typer.echo(render_link_list_json(links))
    elif output_format == "dot":
        typer.echo(render_links_dot(links, focus=(entity_type, entity_id)))
    else:
        typer.echo(render_link_list_md(links))


@graph_app.command("trace")
def graph_trace(
    entity_arg: Annotated[
        str,
        typer.Argument(
            metavar="ENTITY",
            help="entity to trace provenance for, formatted '<entity_type>:<entity_id>'.",
        ),
    ],
    depth: Annotated[
        int,
        typer.Option(
            "--depth",
            "-d",
            help="Maximum number of backward hops (default 3, max 10).",
        ),
    ] = 3,
    type_filter: Annotated[
        list[str] | None,
        typer.Option(
            "--type",
            "-t",
            help="Filter the recursion by link_type (repeatable).",
        ),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: md | json | dot. Defaults to md.",
        ),
    ] = "md",
) -> None:
    """Trace incoming provenance chains backward from ``ENTITY``."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._render import (
        render_link_paths_dot,
        render_link_paths_json,
        render_link_paths_md,
    )
    from opshub.cli._wiring import build_link_service
    from opshub.core.errors import ConfigError, OpsHubError

    _validate_format(output_format)
    entity_type, entity_id = _parse_entity_arg(entity_arg, param_name="ENTITY")

    service = build_link_service(actor="cli:graph_trace")
    try:
        paths = service.trace(
            entity_type,
            entity_id,
            depth=depth,
            link_types=type_filter,
        )
    except ConfigError as exc:
        # Depth ceiling (ADR-0017 §決定 (e)) or uninitialised DB.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if output_format == "json":
        typer.echo(render_link_paths_json(paths))
    elif output_format == "dot":
        typer.echo(render_link_paths_dot(paths, focus=(entity_type, entity_id)))
    else:
        typer.echo(render_link_paths_md(paths, focus=(entity_type, entity_id)))


@graph_app.command("expand")
def graph_expand(
    entity_arg: Annotated[
        str,
        typer.Argument(
            metavar="ENTITY",
            help="entity to expand around, formatted '<entity_type>:<entity_id>'.",
        ),
    ],
    depth: Annotated[
        int,
        typer.Option(
            "--depth",
            "-d",
            help="Maximum number of expansion hops (default 2, max 5).",
        ),
    ] = 2,
    type_filter: Annotated[
        list[str] | None,
        typer.Option(
            "--type",
            "-t",
            help="Filter the expansion by link_type (repeatable).",
        ),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: md | json | dot. Defaults to md.",
        ),
    ] = "md",
) -> None:
    """Return a connected subgraph around ``ENTITY`` (Phase 8 step D2).

    Materialises a bidirectional N-hop
    :class:`~opshub.services.links.GraphSubset` via
    :meth:`LinkService.expand` and renders it in the requested format.
    The depth ceiling (ADR-0017 §決定 (e): max 5) is enforced inside
    the service and surfaces as :class:`ConfigError` here (exit 2).
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._render import (
        render_graph_subset_dot,
        render_graph_subset_json,
        render_graph_subset_md,
    )
    from opshub.cli._wiring import build_link_service
    from opshub.core.errors import ConfigError, OpsHubError

    _validate_format(output_format)
    entity_type, entity_id = _parse_entity_arg(entity_arg, param_name="ENTITY")

    service = build_link_service(actor="cli:graph_expand")
    try:
        subset = service.expand(
            entity_type,
            entity_id,
            depth=depth,
            link_types=type_filter,
        )
    except ConfigError as exc:
        # Depth ceiling (ADR-0017 §決定 (e): max 5, must be >= 0) or
        # uninitialised DB. Mirrors the ``graph trace`` mapping so the
        # operator sees consistent exit codes across the two recursive
        # traversals.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if output_format == "json":
        typer.echo(render_graph_subset_json(subset))
    elif output_format == "dot":
        typer.echo(render_graph_subset_dot(subset))
    else:
        typer.echo(render_graph_subset_md(subset))
