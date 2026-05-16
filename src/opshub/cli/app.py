"""Typer CLI entry point.

Phase 1 step 3 skeleton: provides `opshub version` as a smoke command.
Step 13 added `opshub init` and `opshub db migrate` for first-time setup and
on-demand schema upgrades. Subcommand callbacks defer heavy imports
(``opshub.core``, ``opshub.db``, ``alembic``) to call time so that
``opshub --help`` cold start stays under ~300ms (ADR-0001).
"""

from __future__ import annotations

import typer

from opshub import __version__
from opshub.cli.embeddings import embeddings_app
from opshub.cli.projections import projections_app
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
app.add_typer(workspace_app)


@app.callback()
def _root() -> None:  # pyright: ignore[reportUnusedFunction]
    """Root callback. Required so that single-subcommand mode is not used; this keeps
    `opshub <subcommand>` invocation stable as more commands are added in Phase 1.
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
    """Console script entry point (referenced by [project.scripts].opshub)."""
    app()


if __name__ == "__main__":
    main()
