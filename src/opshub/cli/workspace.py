"""``opshub workspace ...`` subcommands.

Phase 1 step 16 ships ``opshub workspace generate``, which regenerates the
disposable markdown workspace (ADR-0003) from the ``tasks`` projection.

Heavy imports (``opshub.markdown``, ``opshub.core.config``,
``opshub.db``) are deferred to call time so that ``opshub --help`` cold
start stays under the ADR-0001 ~300ms budget; the module-level surface is
limited to Typer and ``__future__``.
"""

from __future__ import annotations

import typer

__all__ = ["workspace_app"]


workspace_app = typer.Typer(
    name="workspace",
    help="Workspace generation commands.",
    no_args_is_help=True,
)


@workspace_app.command("generate")
def workspace_generate() -> None:
    """Regenerate the markdown workspace from the tasks projection."""
    # Lazy import: keep ``opshub --help`` cold start fast (ADR-0001).
    from opshub.cli._wiring import build_engine
    from opshub.core.config import OpsHubSettings
    from opshub.markdown import generate_workspace

    settings = OpsHubSettings()
    engine = build_engine()
    try:
        count = generate_workspace(engine, settings.workspace.root)
    finally:
        engine.dispose()
    typer.echo(f"wrote {count} file(s) under {settings.workspace.root}/generated/tasks")
