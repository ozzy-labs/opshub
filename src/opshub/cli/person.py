"""``opshub person`` — person-axis identity CRUD (Phase 25-B, ADR-0043).

Three subcommands surface the operator-facing view of the person graph
the :class:`~opshub.services.persons.PersonResolutionService` resolves
from the normalised ``sources`` author handles (Phase 25-A):

* ``opshub person list [--format table|json]`` — resolve any not-yet-
  bound author handles into persons, then list every person with its
  bundled identities. The resolve step is incremental + idempotent
  (re-running binds nothing new) so ``list`` is safe to repeat.
* ``opshub person merge <a> <b>`` — operator HITL merge of two persons
  the resolver could only fuzzy-match (display-name similarity is never
  auto-merged, ADR-0043). The lexicographically-smaller id survives.
* ``opshub person split <connector>:<handle>`` — detach one identity
  into a fresh person, undoing an over-eager merge.

Entity references for ``split`` use the ``<connector>:<handle>`` syntax
(e.g. ``slack:U0123`` or ``google_mail:alice@example.com``); the parser
rejects malformed input with :class:`typer.BadParameter` so the operator
sees a clean usage error.

Cold-start guard
----------------
Module-level imports stay within the cold-start whitelist (``__future__``
/ ``typer`` / ``typing``). Every ``opshub`` import lives inside a command
body so the cold-start budget is paid only when a person subcommand runs
(ADR-0001).

Exit-code contract
------------------
* ``0`` — success.
* ``1`` — :class:`~opshub.core.errors.OpsHubError` raised by the service
  (e.g. merging a person with itself, merging / splitting a missing id).
* ``2`` — :class:`~opshub.core.errors.ConfigError` (DB not initialised)
  or :class:`typer.BadParameter` (malformed identity argument).
"""

from __future__ import annotations

from typing import Annotated

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

person_app = typer.Typer(
    name="person",
    help="Manage the person-axis identity graph (list / merge / split).",
    no_args_is_help=True,
)


def _parse_identity_arg(value: str, *, param_name: str) -> tuple[str, str]:
    """Parse ``<connector>:<handle>`` into a tuple.

    The first ``:`` is the delimiter — email handles embed no colon but
    Slack handles never do either, so the split stays unambiguous. An
    empty connector or handle raises :class:`typer.BadParameter`.
    """
    if ":" not in value:
        raise typer.BadParameter(
            f"expected '<connector>:<handle>' (e.g. 'slack:U0123'), got {value!r}",
            param_hint=param_name,
        )
    connector, _, handle = value.partition(":")
    if not connector or not handle:
        raise typer.BadParameter(
            f"expected non-empty connector and handle in '<connector>:<handle>', got {value!r}",
            param_hint=param_name,
        )
    return connector, handle


def _validate_format(value: str) -> None:
    """Reject an unsupported ``--format`` value with exit code 2."""
    if value not in {"table", "json"}:
        raise typer.BadParameter(
            f"unsupported format {value!r}; choose 'table' or 'json'.",
            param_hint="--format",
        )


@person_app.command("list")
def person_list(
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: table | json.",
        ),
    ] = "table",
) -> None:
    """Resolve author handles into persons and list them.

    Resolution is incremental + idempotent — already-bound handles are
    skipped — so this command can be run repeatedly. Persons appear
    newest-first; each row lists the operator flag and the bundled
    ``<connector>:<handle>`` identities.
    """
    import json as _json

    from opshub.cli._wiring import build_person_service
    from opshub.core.errors import ConfigError

    _validate_format(fmt)

    service = build_person_service()
    try:
        service.resolve()
        persons = service.list_persons()
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if fmt == "json":
        payload = [
            {
                "id": p.id,
                "display_name": p.display_name,
                "is_operator": p.is_operator,
                "identities": [
                    {
                        "connector": i.connector,
                        "handle": i.handle,
                        "display": i.display,
                        "confidence": i.confidence,
                    }
                    for i in p.identities
                ],
            }
            for p in persons
        ]
        typer.echo(_json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not persons:
        typer.echo("(no persons resolved yet — run a connector sync first)")
        return

    for p in persons:
        flag = " [operator]" if p.is_operator else ""
        idents = ", ".join(f"{i.connector}:{i.handle}" for i in p.identities) or "(none)"
        typer.echo(f"{p.id}  {p.display_name}{flag}")
        typer.echo(f"    identities: {idents}")


@person_app.command("merge")
def person_merge(
    person_a: Annotated[
        str,
        typer.Argument(metavar="PERSON_A", help="first person id (ULID)."),
    ],
    person_b: Annotated[
        str,
        typer.Argument(metavar="PERSON_B", help="second person id (ULID)."),
    ],
) -> None:
    """Merge two persons into one (operator HITL, ADR-0043).

    The lexicographically-smaller id survives so the result is
    deterministic regardless of argument order. Emits
    :class:`~opshub.domain.events.IdentityMerged` and re-parents every
    identity of the merged person onto the survivor.
    """
    from opshub.cli._wiring import build_person_service
    from opshub.core.errors import ConfigError, OpsHubError

    service = build_person_service(actor="cli:person_merge")
    try:
        survivor = service.merge(person_a, person_b)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Merged {person_a} + {person_b} -> {survivor}")


@person_app.command("split")
def person_split(
    identity: Annotated[
        str,
        typer.Argument(
            metavar="IDENTITY",
            help="identity to detach, formatted '<connector>:<handle>' (e.g. 'slack:U0123').",
        ),
    ],
) -> None:
    """Detach one identity into a fresh person (operator HITL, ADR-0043).

    Emits :class:`~opshub.domain.events.IdentitySplit`; the
    ``<connector>:<handle>`` identity is repointed onto a freshly-minted
    person whose id is echoed on stdout.
    """
    from opshub.cli._wiring import build_person_service
    from opshub.core.errors import ConfigError, OpsHubError

    connector, handle = _parse_identity_arg(identity, param_name="IDENTITY")

    service = build_person_service(actor="cli:person_split")
    try:
        new_id = service.split(connector, handle)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Split {identity} -> new person {new_id}")
