"""``opshub agent ...`` subcommands (Phase 2 step 6).

The agent surface is namespaced under ``opshub agent run ...`` because
agent runs are the only agent-scoped resource Phase 2 ships. Two
commands cover the lifecycle:

* ``opshub agent run begin <agent-name> [--session ...] [--actor ...]``
  — start an agent run. Prints the run's 26-char ULID on stdout.
  ``--session`` is resolved through
  :func:`opshub.cli._actor.resolve_owner`, so when omitted the env
  var / state-file fallback applies.
* ``opshub agent run end <run-id> [--summary ...] [--actor ...]``
  — end an active agent run, optionally recording a summary.

Module-level imports stay limited to ``__future__`` + ``typer`` so
``opshub --help`` cold start stays under the ADR-0001 budget. Heavy
imports load lazily inside each callback.
"""

from __future__ import annotations

import typer

agent_app = typer.Typer(name="agent", help="Agent commands.", no_args_is_help=True)

run_app = typer.Typer(name="run", help="Agent run lifecycle.", no_args_is_help=True)
agent_app.add_typer(run_app)


@run_app.command("begin")
def agent_run_begin(
    agent_name: str = typer.Argument(..., help="Agent identifier (e.g. 'claude', 'codex')."),
    session: str | None = typer.Option(
        None,
        "--session",
        help=("Parent work session ULID. Defaults to $OPSHUB_WORK_SESSION_ID or the state file."),
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Override the recorded actor (defaults to $OPSHUB_ACTOR or 'cli:default').",
    ),
) -> None:
    """Begin a new agent run and print its ULID.

    Stdout contains exactly one line: the 26-character ULID of the
    new agent run. The agent run is linked to its parent work session
    via ``work_session_id`` when one is resolvable.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._actor import resolve_owner
    from opshub.cli._wiring import build_agent_run_service

    owner = resolve_owner(actor=actor, work_session_id=session)
    service = build_agent_run_service(owner.actor)
    event = service.begin(agent_name=agent_name, work_session_id=owner.work_session_id)
    typer.echo(event.aggregate_id)


@run_app.command("end")
def agent_run_end(
    run_id: str = typer.Argument(..., help="The agent run ULID to end."),
    summary: str | None = typer.Option(
        None,
        "--summary",
        help="Optional summary recorded on the event.",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Override the recorded actor (defaults to $OPSHUB_ACTOR or 'cli:default').",
    ),
) -> None:
    """End an active agent run."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._actor import resolve_owner
    from opshub.cli._wiring import build_agent_run_service

    owner = resolve_owner(actor=actor)
    service = build_agent_run_service(owner.actor)
    service.end(run_id=run_id, summary=summary)
    typer.echo(f"ended {run_id}")
