"""``opshub projections ...`` subcommands.

Phase 1 step 15 ships a single ``projections rebuild`` subcommand that
empties every registered projection table and replays the entire event
log through them in order (ADR-0002 / projections are disposable).

The user-facing report counts both the number of events replayed and the
number of projections that were reset; that's the minimum useful signal
when debugging a divergent read model.

Module-level imports stay limited to ``__future__`` plus ``typer`` so
``opshub --help`` cold start does not pay for SQLAlchemy / pydantic
(ADR-0001 lazy-import rule).
"""

from __future__ import annotations

import typer

projections_app = typer.Typer(
    name="projections",
    help="Projection operations.",
    no_args_is_help=True,
)


@projections_app.command("rebuild")
def projections_rebuild() -> None:
    """Reset and replay every projection from the events table."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli import _progress
    from opshub.cli._wiring import build_engine
    from opshub.db import SqlAlchemyEventStore
    from opshub.projections import all_projections, rebuild_all

    engine = build_engine()
    store = SqlAlchemyEventStore(engine)
    # The registry is the single source of truth for "which projections
    # OpsHub knows about". The inline projector used by the task service
    # reads the same function, so the two paths can never drift.
    projections = all_projections()

    # Count events up front. ``iter_all`` is a generator backed by a
    # streaming SELECT, so this is one read pass; ``rebuild_all`` will
    # do its own independent pass during replay. The count also sizes the
    # determinate progress bar (renders on stderr, no-ops on non-TTY).
    n_events = sum(1 for _ in store.iter_all())

    with _progress.determinate(n_events, "rebuilding projections") as reporter:
        rebuild_all(engine, store, projections, progress_callback=reporter.advance)
    typer.echo(f"rebuilt {len(projections)} projection(s) from {n_events} event(s)")
