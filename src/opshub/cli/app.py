"""Typer CLI entry point.

Phase 1 step 3 skeleton: provides `opshub version` as a smoke command.
Subcommands (task / event / projections / workspace / embeddings / ...) are
added in subsequent Phase 1 commits (see docs/phase-1-plan.md §2).
"""

from __future__ import annotations

import typer

from opshub import __version__

app = typer.Typer(
    name="opshub",
    help="Local-first operational memory and execution hub for humans and AI agents.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:  # pyright: ignore[reportUnusedFunction]
    """Root callback. Required so that single-subcommand mode is not used; this keeps
    `opshub <subcommand>` invocation stable as more commands are added in Phase 1.
    """


@app.command()
def version() -> None:
    """Show the installed opshub version."""
    typer.echo(f"opshub {__version__}")


def main() -> None:
    """Console script entry point (referenced by [project.scripts].opshub)."""
    app()


if __name__ == "__main__":
    main()
