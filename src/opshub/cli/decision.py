"""``opshub decision ...`` subcommands.

Phase 2 step 4 ships two decision commands wired against the SQLAlchemy
event store and the ``decisions`` read-model projection:

* ``opshub decision record "<text>" [--context <text>] [--actor <name>]``
  — appends a :class:`~opshub.domain.events.DecisionRecorded` event via
  :class:`~opshub.services.decision_service.DecisionService` and prints
  the new decision ULID on stdout. The ULID is the only thing on stdout
  so callers can pipe it into follow-up commands.
* ``opshub decision list [--format md|json|table]`` — queries the
  ``decisions`` projection table and renders the rows in the requested
  format. Rows are sorted by ``recorded_at DESC, id ASC``.

Module-level imports are restricted to ``__future__`` and ``typer`` so
that ``opshub --help`` cold start stays under the ~300ms budget set by
ADR-0001; heavy modules (SQLAlchemy, Pydantic settings, the service
layer) load lazily inside each command callback when it actually runs.

Rendering: Phase 2 step 3 introduces a shared ``cli/_render.py`` helper
that ``decision list`` is intended to consume once step 3 merges. While
that PR is still open we keep a minimal inline renderer here (TODO
below) so this PR does not block on step 3 — the migration to the shared
module is a mechanical rebase once both land.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

decision_app = typer.Typer(
    name="decision",
    help="Decision recording.",
    no_args_is_help=True,
)


@decision_app.command("record")
def decision_record(
    text: str = typer.Argument(..., help="Decision text (1..2000 chars)."),
    context: str | None = typer.Option(
        None,
        "--context",
        help="Optional supporting prose for the decision.",
    ),
    actor: str = typer.Option(
        "cli:decision",
        "--actor",
        help="Actor recorded on the event.",
    ),
) -> None:
    """Record a new decision and print its ULID.

    Stdout contains exactly one line: the 26-character ULID assigned to
    the new decision. The same ULID is written to the ``aggregate_id``
    column on the appended
    :class:`~opshub.domain.events.DecisionRecorded` event.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_decision_service

    service = build_decision_service(actor)
    event = service.record_decision(text=text, context=context)
    typer.echo(event.aggregate_id)


@decision_app.command("list")
def decision_list(
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: md | json | table.",
    ),
) -> None:
    """List decisions from the ``decisions`` projection.

    Rows are sorted by ``recorded_at DESC, id ASC`` so the most recently
    recorded decisions appear first. The optional ``--format`` flag
    picks the rendering; an invalid value raises
    :class:`~opshub.core.errors.ValidationError` which the CLI maps to a
    non-zero exit with a readable message.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    # TODO(step-3 rebase): migrate to ``opshub.cli._render`` once step 3
    # lands (`render_table` / `render_json` / `render_md`). Until then
    # we ship a minimal inline renderer here so this step does not
    # block on step 3.
    from opshub.cli._decision_list import render_decision_list
    from opshub.cli._wiring import build_engine

    engine = build_engine()
    output = render_decision_list(engine, fmt=fmt)
    typer.echo(output)
