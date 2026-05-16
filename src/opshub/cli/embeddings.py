"""``opshub embeddings ...`` subcommands.

Phase 1 step 15 ships a single ``embeddings status`` subcommand: a stub
that reports the configured backend (always ``"disabled"`` in Phase 1
unless the user overrides ``OPSHUB_EMBEDDING__BACKEND``) and the current
row count of the ``embeddings`` table.

This command exists so the embeddings story is observable from day one
even though Phase 1 does not yet write rows to the table; Phase 4 will
extend it with backend health / model identity details.

Module-level imports stay limited to ``__future__`` plus ``typer`` so
``opshub --help`` cold start stays under the ADR-0001 budget.
"""

from __future__ import annotations

import typer

embeddings_app = typer.Typer(
    name="embeddings",
    help="Embedding operations.",
    no_args_is_help=True,
)


@embeddings_app.command("status")
def embeddings_status() -> None:
    """Show the configured embedding backend and current row count."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from sqlalchemy import text

    from opshub.cli._wiring import build_engine
    from opshub.core.config import OpsHubSettings

    settings = OpsHubSettings()
    engine = build_engine()
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM embeddings")).scalar_one()

    typer.echo(f"backend={settings.embedding.backend}")
    typer.echo(f"embeddings: {count} rows")
