"""``opshub handoff ...`` subcommands.

Phase 2 step 7 ships three handoff commands wired against the SQLAlchemy
event store and the ``handoffs`` read-model projection:

* ``opshub handoff open --from <a> --to <b> --topic "<text>"`` —
  appends :class:`~opshub.domain.events.HandoffOpened` via
  :class:`~opshub.services.handoff_service.HandoffService` and prints
  the new handoff ULID on stdout.
* ``opshub handoff close <id> [--note <text>]`` — appends
  :class:`~opshub.domain.events.HandoffClosed` and transitions the
  projection row to ``closed``.
* ``opshub handoff list [--format md|json|table]`` — queries the
  ``handoffs`` projection for every row whose state is ``open``.

Module-level imports are restricted to ``__future__`` and ``typer`` so
that ``opshub --help`` cold start stays under the ~300ms budget set
by ADR-0001; heavy modules (SQLAlchemy, Pydantic settings, the service
layer) load lazily inside each command callback.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

handoff_app = typer.Typer(
    name="handoff",
    help="Coordination handoffs.",
    no_args_is_help=True,
)


@handoff_app.command("open")
def handoff_open(
    from_actor: str = typer.Option(..., "--from", help="Actor passing the work."),
    to_actor: str = typer.Option(..., "--to", help="Actor receiving the work."),
    topic: str = typer.Option(..., "--topic", help="Short subject line (1..200 chars)."),
    actor: str = typer.Option(
        "cli:handoff",
        "--actor",
        help="Actor recorded on the event.",
    ),
) -> None:
    """Open a new handoff and print its ULID.

    Stdout contains exactly one line: the 26-character ULID assigned
    to the new handoff so subsequent ``opshub handoff close <id>``
    invocations can refer to it.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_handoff_service

    service = build_handoff_service(actor)
    event = service.open(from_actor=from_actor, to_actor=to_actor, topic=topic)
    typer.echo(event.aggregate_id)


@handoff_app.command("close")
def handoff_close(
    handoff_id: str = typer.Argument(..., help="The handoff ULID to close."),
    note: str | None = typer.Option(None, "--note", help="Optional closing note."),
    actor: str = typer.Option(
        "cli:handoff",
        "--actor",
        help="Actor recorded on the event.",
    ),
) -> None:
    """Close an open handoff, optionally recording a closing note."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_handoff_service

    service = build_handoff_service(actor)
    service.close(handoff_id=handoff_id, note=note)


@handoff_app.command("list")
def handoff_list(
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: md | json | table.",
    ),
) -> None:
    """List every open handoff in the configured workspace.

    Rows are sorted by ``opened_at DESC, id ASC``. ``--format`` picks
    the rendering. An invalid ``--format`` raises
    :class:`~opshub.core.errors.ValidationError` which Typer surfaces
    as a non-zero exit with a readable message.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._handoff_render import render_open_handoffs
    from opshub.cli._wiring import build_handoff_service

    service = build_handoff_service(actor="cli:handoff")
    rows = service.list_open()
    typer.echo(render_open_handoffs(rows, fmt=fmt))
