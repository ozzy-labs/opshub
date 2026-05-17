"""``opshub link`` — manual link CRUD (Phase 8 step D1, ADR-0017 §決定 (d)).

Three subcommands surface the operator-facing manual link CRUD on
top of the writer-side :class:`LinkService` extension (Phase 8 D1):

* ``opshub link add <from> <to> --type <link-type> [--metadata k=v ...]``
* ``opshub link remove <link-id> [--reason "..."]``
* ``opshub link list [--from <entity>] [--to <entity>] [--type <type>]
  [--limit N] [--format md|json]``

Entity references use the ``<entity_type>:<entity_id>`` syntax (e.g.
``task:01J...`` or ``source:01J...``) and the parser rejects malformed
input with :class:`typer.BadParameter` so the operator sees a clean
usage error instead of a stack trace.

Auto-extraction parity
----------------------

Manual links flow through the event log via :class:`LinkCreated` /
:class:`LinkDeleted` events (per ADR-0002 + ADR-0017 §決定 (d));
auto-extracted links are written directly by the Phase 8 B2
``LinksExtractor`` projector and do NOT come through this CLI.

The CLI accepts an arbitrary ``--type`` string (defaulting to
``manual``) so an operator can also assert one of the semantic types
the auto-extractor uses (e.g. ``--type references`` for an early
manual source reference before the connector catches up). The
projector treats both populations the same; the audit trail
distinguishes them via the ``LinkCreated`` event (manual) vs. the
derived link's ``source_event_id`` (auto-extracted).

Cold-start guard
----------------

Module-level imports stay within the M6 cold-start whitelist
(``__future__`` / ``typer`` / ``typing``). Every ``opshub`` import
lives inside the command function body so the cold-start budget is
paid only when the operator actually invokes a link subcommand
(ADR-0001).

Exit-code contract
------------------

* ``0`` — success (including ``link remove`` of a non-existent id —
  the audit event is appended either way; the success message notes
  ``(no row)`` for the no-op case).
* ``1`` — :class:`~opshub.core.errors.OpsHubError` raised by the
  service (e.g. validation failure deeper in the stack).
* ``2`` — :class:`~opshub.core.errors.ConfigError` (DB not
  initialised) or :class:`typer.BadParameter` (malformed entity
  argument / metadata syntax). Typer maps ``BadParameter`` to exit 2
  on its own; ``ConfigError`` we re-map manually for symmetry with
  the rest of the CLI.
"""

from __future__ import annotations

from typing import Annotated

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

link_app = typer.Typer(
    name="link",
    help="Manage entity links manually (add / remove / list).",
    no_args_is_help=True,
)


def _parse_entity_arg(value: str, *, param_name: str) -> tuple[str, str]:
    """Parse ``<entity_type>:<entity_id>`` into a tuple.

    ``typer.BadParameter`` is raised on a missing colon, an empty
    type, or an empty id so the operator sees ``Error: Invalid value
    for '<from>': ...`` rather than a downstream ``ValidationError``
    stack trace. The first ``:`` is the delimiter — entity ids may
    embed further colons (none currently do, but the parse stays
    forward-compatible).
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


def _parse_metadata_pairs(pairs: list[str] | None) -> dict[str, str] | None:
    """Parse a list of ``k=v`` strings into a ``dict[str, str]`` or ``None``.

    ``None`` / empty list returns ``None`` so the event's ``metadata``
    field stays ``None`` (matches the ``LinkCreated.metadata`` default
    semantics — absence means "no extra context"). Duplicate keys take
    the last-seen value, which mirrors common CLI flag behaviour.
    Each pair must contain exactly one ``=``; malformed entries raise
    :class:`typer.BadParameter`.
    """
    if not pairs:
        return None
    parsed: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise typer.BadParameter(
                f"expected 'key=value' pairs for --metadata, got {raw!r}",
                param_hint="--metadata",
            )
        key, _, val = raw.partition("=")
        if not key:
            raise typer.BadParameter(
                f"--metadata key must be non-empty, got {raw!r}",
                param_hint="--metadata",
            )
        parsed[key] = val
    return parsed


@link_app.command("add")
def link_add(
    from_arg: Annotated[
        str,
        typer.Argument(
            metavar="FROM",
            help="from entity, formatted '<entity_type>:<entity_id>' (e.g. 'task:01J...').",
        ),
    ],
    to_arg: Annotated[
        str,
        typer.Argument(
            metavar="TO",
            help="to entity, formatted '<entity_type>:<entity_id>'.",
        ),
    ],
    link_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help="link_type label (default 'manual'; see ADR-0017 §決定 (b) for the auto-enum).",
        ),
    ] = "manual",
    metadata_pairs: Annotated[
        list[str] | None,
        typer.Option(
            "--metadata",
            "-m",
            help="Optional 'key=value' metadata pairs (repeatable).",
        ),
    ] = None,
) -> None:
    """Add a manual link between two entities.

    Emits :class:`LinkCreated` and applies the
    :class:`LinksProjector` (UPSERT) in one UoW. The new link id is
    echoed on stdout for piping into a subsequent ``link remove``.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_link_service
    from opshub.core.errors import ConfigError, OpsHubError

    from_entity_type, from_entity_id = _parse_entity_arg(from_arg, param_name="FROM")
    to_entity_type, to_entity_id = _parse_entity_arg(to_arg, param_name="TO")
    metadata = _parse_metadata_pairs(metadata_pairs)

    service = build_link_service(actor="cli:link_add")
    try:
        link_id = service.create_link(
            from_entity_type=from_entity_type,
            from_entity_id=from_entity_id,
            to_entity_type=to_entity_type,
            to_entity_id=to_entity_id,
            link_type=link_type,
            metadata=metadata,
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    short_id = link_id[-6:] if len(link_id) >= 6 else link_id
    typer.echo(f"Link {short_id} added: {from_arg} -> {to_arg} (type={link_type}, id={link_id})")


@link_app.command("remove")
def link_remove(
    link_id: Annotated[
        str,
        typer.Argument(help="link id (ULID) emitted by 'opshub link add' or 'opshub link list'."),
    ],
    reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            "-r",
            help="Optional free-form audit reason (sanitised before persistence).",
        ),
    ] = None,
) -> None:
    """Remove a manual link by id.

    Emits :class:`LinkDeleted` which the projector hard-deletes the
    row (ADR-0017 §決定 (h)). The event is appended even when the
    row was already absent (e.g. an auto-extracted link that was
    collapsed by a later UPSERT) so the audit trail is preserved.

    Auto-extracted links (whose id is derived from a natural-key
    hash) can technically be removed this way too, but they will be
    re-derived on the next ``projections rebuild`` — operators
    wanting to suppress auto-links should instead modify the
    upstream event or wait for a future Phase 8.x ``link suppress``
    flag.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_link_service
    from opshub.core.errors import ConfigError, OpsHubError

    service = build_link_service(actor="cli:link_remove")
    try:
        deleted = service.delete_link(link_id, reason=reason)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    short_id = link_id[-6:] if len(link_id) >= 6 else link_id
    if deleted:
        typer.echo(f"Link {short_id} removed.")
    else:
        typer.echo(f"Link {short_id} not found (no-op); audit event appended.")


@link_app.command("list")
def link_list(
    from_filter: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Filter by from entity, formatted '<entity_type>:<entity_id>'.",
        ),
    ] = None,
    to_filter: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="Filter by to entity, formatted '<entity_type>:<entity_id>'.",
        ),
    ] = None,
    type_filter: Annotated[
        str | None,
        typer.Option(
            "--type",
            "-t",
            help="Filter by link_type (single value).",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Maximum number of links to display (default 50).",
        ),
    ] = 50,
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: md | json. Defaults to md.",
        ),
    ] = "md",
) -> None:
    """List links with optional filters.

    Filters are AND-combined. ``--from`` / ``--to`` accept the same
    ``<entity_type>:<entity_id>`` syntax as ``link add``. The rendered
    output mirrors the :class:`Link` dataclass shape — ``md`` produces
    a Markdown table, ``json`` produces a JSON array suitable for
    piping into ``jq``.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._render import render_link_list_json, render_link_list_md
    from opshub.cli._wiring import build_link_service

    if output_format not in {"md", "json"}:
        typer.echo(
            f"Error: invalid --format {output_format!r}; expected one of 'md', 'json'",
            err=True,
        )
        raise typer.Exit(code=2)

    from_entity_type: str | None = None
    from_entity_id: str | None = None
    if from_filter is not None:
        from_entity_type, from_entity_id = _parse_entity_arg(from_filter, param_name="--from")
    to_entity_type: str | None = None
    to_entity_id: str | None = None
    if to_filter is not None:
        to_entity_type, to_entity_id = _parse_entity_arg(to_filter, param_name="--to")

    service = build_link_service(actor="cli:link_list")
    links = service.list_links(
        from_entity_type=from_entity_type,
        from_entity_id=from_entity_id,
        to_entity_type=to_entity_type,
        to_entity_id=to_entity_id,
        link_type=type_filter,
        limit=limit,
    )

    if output_format == "json":
        typer.echo(render_link_list_json(links))
    else:
        typer.echo(render_link_list_md(links))
