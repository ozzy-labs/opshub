"""Typer CLI entry point.

Phase 1 step 3 skeleton: provides `opshub version` as a smoke command.
Step 13 added `opshub init` and `opshub db migrate` for first-time setup and
on-demand schema upgrades. Subcommand callbacks defer heavy imports
(``opshub.core``, ``opshub.db``, ``alembic``) to call time so that
``opshub --help`` cold start stays under ~300ms (ADR-0001).

Phase 2 step 1 adds a top-level :class:`OpsHubError` handler in
:func:`main` (the console-script entry point). Domain failures surface
as a single ``Error: <message>`` line on stderr plus a meaningful exit
code, rather than leaking a Python traceback at the user. The wrapper
only sits on :func:`main` so existing tests using
``typer.testing.CliRunner`` (which invokes ``app`` directly) still see
the raw exception on ``result.exception``.
"""

from __future__ import annotations

from typing import Annotated

import typer

from opshub import __version__
from opshub.cli.agent import agent_app
from opshub.cli.brief import register as register_brief
from opshub.cli.connector import connector_app
from opshub.cli.decision import decision_app
from opshub.cli.embeddings import embeddings_app
from opshub.cli.graph import graph_app
from opshub.cli.handoff import handoff_app
from opshub.cli.inbox import inbox_app
from opshub.cli.link import link_app
from opshub.cli.lock import lock_app
from opshub.cli.projections import projections_app
from opshub.cli.propose import propose_app
from opshub.cli.recall import register as register_recall
from opshub.cli.search import register as register_search
from opshub.cli.session import session_app
from opshub.cli.task import task_app
from opshub.cli.workspace import workspace_app

app = typer.Typer(
    name="opshub",
    help="Local-first operational memory and execution hub for humans and AI agents.",
    no_args_is_help=True,
)

db_app = typer.Typer(
    name="db",
    help="Database operations.",
    no_args_is_help=True,
)
app.add_typer(db_app)
app.add_typer(projections_app)
app.add_typer(embeddings_app)
app.add_typer(task_app)
app.add_typer(inbox_app)
app.add_typer(decision_app)
app.add_typer(lock_app)
app.add_typer(handoff_app)
app.add_typer(session_app)
app.add_typer(agent_app)
app.add_typer(workspace_app)
app.add_typer(connector_app)
app.add_typer(propose_app)
app.add_typer(link_app)
app.add_typer(graph_app)
register_recall(app)
register_search(app)
register_brief(app)


def _version_callback(value: bool) -> None:
    """Eager ``--version`` handler: echo the package version and exit."""
    if value:
        typer.echo(f"opshub {__version__}")
        raise typer.Exit()


@app.callback()
def _root(  # pyright: ignore[reportUnusedFunction]
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed opshub version and exit.",
        ),
    ] = None,
) -> None:
    """Root callback. Required so that single-subcommand mode is not used; this keeps
    `opshub <subcommand>` invocation stable as more commands are added in Phase 1.

    The ``--version`` flag is wired here (rather than as a separate command)
    so that ``opshub --version`` is recognised before any subcommand
    parsing. The existing ``opshub version`` subcommand is preserved
    below and produces identical output.
    """


@app.command()
def version() -> None:
    """Show the installed opshub version."""
    typer.echo(f"opshub {__version__}")


@app.command("init")
def init(
    *,
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing config.toml with the starter template.",
    ),
) -> None:
    """First-time setup: create dirs, write starter config, apply migrations."""
    # Lazy import: heavy modules (pydantic_settings, alembic) load only when
    # the command actually runs.
    from opshub.cli.init import init_command

    init_command(force=force)


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending Alembic migrations."""
    # Lazy import: see module docstring.
    from opshub.cli.db import migrate_command

    migrate_command()


def main() -> None:
    """Console script entry point (referenced by ``[project.scripts].opshub``).

    Wraps the Typer ``app`` so that :class:`OpsHubError` subclasses surface
    as a single ``Error: <message>`` line on stderr plus a meaningful exit
    code, rather than leaking a Python traceback at the user:

    * :class:`ValidationError` (bad command input) → exit code 2 — mirrors
      the conventional Unix "usage error" code that Typer itself returns
      for argument parsing failures.
    * Any other :class:`OpsHubError` (config / not-found / conflict) →
      exit code 1 — the generic "command failed" code.

    The catch order matters: :class:`ValidationError` is a subclass of
    :class:`OpsHubError`, so the narrower handler must come first.
    Unexpected (non-OpsHub) exceptions are deliberately left to
    propagate so a real bug still shows its traceback in CI logs.

    Implementation notes:

    * ``SystemExit`` is raised (not ``typer.Exit``) because the Typer /
      Click runtime is no longer on the stack by the time ``app()`` has
      returned or raised — ``typer.Exit`` is a ``RuntimeError`` subclass
      that only Click's runtime knows how to translate into an exit
      code. Raising ``SystemExit`` directly is the idiomatic way to
      surface a process exit code from a plain ``main()`` entry point.
    * The exception types are imported lazily so that
      ``tests/integration/test_cli_imports.py`` can keep
      ``opshub.cli.app``'s module-level surface limited to ``typer``
      and the sub-app objects (ADR-0001 cold-start discipline).
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001) and satisfy the
    # ``test_cli_imports`` static check that bans top-level
    # ``opshub.core`` imports inside ``opshub.cli.*``.
    from opshub.core.errors import OpsHubError, ValidationError

    try:
        app()
    except ValidationError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
