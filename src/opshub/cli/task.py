"""``opshub task ...`` subcommands.

Phase 1 step 14 ships two task commands wired against the SQLAlchemy event
store and the ``tasks`` read-model projection:

* ``opshub task create "<title>" [--body <text>] [--actor <name>]`` — appends
  a :class:`~opshub.domain.events.TaskCreated` event via
  :class:`~opshub.services.task_service.TaskService` and prints the new task
  ULID on stdout. The ULID is the only thing on stdout so callers can pipe it
  into ``opshub task ...`` follow-up commands.
* ``opshub task list [--format md|json|table] [--state ...]`` — queries the
  ``tasks`` projection table and renders the rows in the requested format.

Module-level imports are restricted to ``__future__`` and ``typer`` so that
``opshub --help`` cold start stays under the ~300ms budget set by ADR-0001;
heavy modules (SQLAlchemy, Pydantic settings, the service layer) load lazily
inside each command callback when it actually runs.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

task_app = typer.Typer(name="task", help="Task commands.", no_args_is_help=True)


@task_app.command("create")
def task_create(
    title: str = typer.Argument(..., help="Task title (1..200 chars)."),
    body: str | None = typer.Option(None, "--body", help="Optional task body."),
    actor: str = typer.Option("cli:create", "--actor", help="Actor recorded on the event."),
) -> None:
    """Create a new task and print its ULID.

    Stdout contains exactly one line: the 26-character ULID assigned to the
    new task. The same ULID is written to the ``aggregate_id`` column on the
    appended :class:`~opshub.domain.events.TaskCreated` event so subsequent
    state-transition commands can refer to the task by that id.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_task_service

    service = build_task_service(actor)
    event = service.create_task(title=title, body=body)
    typer.echo(event.aggregate_id)


@task_app.command("list")
def task_list(
    fmt: str = typer.Option("table", "--format", "-f", help="Output format: md | json | table."),
    state: str | None = typer.Option(
        None,
        "--state",
        help="Filter by state (draft | active | completed).",
    ),
) -> None:
    """List tasks from the ``tasks`` projection.

    Rows are sorted by ``updated_at DESC, id ASC``. The optional ``--state``
    flag filters rows server-side; ``--format`` picks the rendering. An
    invalid ``--format`` raises :class:`~opshub.core.errors.ValidationError`
    which Typer surfaces as a non-zero exit with a readable message.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._task_list import render_task_list
    from opshub.cli._wiring import build_engine

    engine = build_engine()
    output = render_task_list(engine, fmt=fmt, state_filter=state)
    typer.echo(output)
