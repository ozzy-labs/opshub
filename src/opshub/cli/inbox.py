"""``opshub inbox ...`` subcommands.

Phase 2 step 3 ships three inbox commands wired against the SQLAlchemy
event store and the ``inbox_items`` read-model projection:

* ``opshub inbox add "<summary>" [--source-ref <ref>] [--actor <name>]``
  — appends an :class:`~opshub.domain.events.ItemEnqueued` event via
  :class:`~opshub.services.inbox_service.InboxService` and prints the
  new item ULID on stdout. The ULID is the only thing on stdout so
  callers can pipe it into ``opshub inbox triage`` follow-up commands.
* ``opshub inbox list [--format md|json|table] [--state ...]`` — queries
  the ``inbox_items`` projection table and renders the rows in the
  requested format.
* ``opshub inbox triage <item_id> --to-task|--decision|--discard ...``
  — disposes of an inbox item by promoting it to a task, marking it
  for a decision, or discarding it with a reason.

Module-level imports are restricted to ``__future__`` and ``typer`` so
``opshub --help`` cold start stays under the ~300ms budget set by
ADR-0001; heavy modules (SQLAlchemy, Pydantic settings, the service
layer) load lazily inside each command callback when it actually runs.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

inbox_app = typer.Typer(name="inbox", help="Inbox triage commands.", no_args_is_help=True)


@inbox_app.command("add")
def inbox_add(
    summary: str = typer.Argument(..., help="Inbox item summary (1..500 chars)."),
    source_ref: str | None = typer.Option(
        None,
        "--source-ref",
        help="Optional external reference (e.g. Slack permalink, GitHub URL).",
    ),
    actor: str = typer.Option(
        "cli:enqueue",
        "--actor",
        help="Actor recorded on the event.",
    ),
) -> None:
    """Capture a new inbox item and print its ULID.

    Stdout contains exactly one line: the 26-character ULID assigned to
    the new item. The same ULID is written to the ``aggregate_id``
    column on the appended :class:`~opshub.domain.events.ItemEnqueued`
    event so ``opshub inbox triage`` can refer to the item by that id.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_inbox_service

    service = build_inbox_service(actor)
    event = service.enqueue(summary=summary, source_ref=source_ref)
    typer.echo(event.aggregate_id)


@inbox_app.command("list")
def inbox_list(
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: md | json | table.",
    ),
    state: str | None = typer.Option(
        None,
        "--state",
        help=("Filter by state (pending | triaged_to_task | triaged_to_decision | discarded)."),
    ),
) -> None:
    """List inbox items from the ``inbox_items`` projection.

    Rows are sorted by ``created_at DESC, id ASC`` so the most recent
    captures appear at the top. ``--state`` filters server-side;
    ``--format`` picks the rendering. An invalid value raises
    :class:`~opshub.core.errors.ValidationError` which Typer surfaces
    as a non-zero exit with a readable message.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._inbox_list import render_inbox_list
    from opshub.cli._wiring import build_engine

    engine = build_engine()
    output = render_inbox_list(engine, fmt=fmt, state_filter=state)
    typer.echo(output)


@inbox_app.command("triage")
def inbox_triage(
    item_id: str = typer.Argument(..., help="Inbox item ULID."),
    to_task: str | None = typer.Option(
        None,
        "--to-task",
        help="Promote the item to a task with the given title.",
    ),
    decision: str | None = typer.Option(
        None,
        "--decision",
        help="Mark the item as triaged to a (future) decision; reason note.",
    ),
    discard: str | None = typer.Option(
        None,
        "--discard",
        help="Discard the item with a free-form reason.",
    ),
    actor: str = typer.Option(
        "cli:triage",
        "--actor",
        help="Actor recorded on the event(s).",
    ),
) -> None:
    """Dispose of an inbox item.

    Exactly one of ``--to-task`` / ``--decision`` / ``--discard`` must
    be provided. The service raises
    :class:`~opshub.core.errors.ValidationError` for 0 or 2+ which the
    CLI maps to exit code 2.

    On success, stdout contains exactly one line: the ULID of the
    triage target — the new task's ULID for ``--to-task``, the
    pre-allocated decision ULID for ``--decision``, or the item's own
    ULID for ``--discard`` (since there is no downstream target).
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_inbox_service

    service = build_inbox_service(actor)
    event = service.triage(
        item_id,
        to_task=to_task,
        decision=decision,
        discard=discard,
    )
    # ``target_id`` is None for discard; surface the item's own id in
    # that case so the command always echoes something useful.
    typer.echo(event.target_id or event.aggregate_id)
