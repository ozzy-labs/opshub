"""``opshub workspace ...`` subcommands.

Phase 1 step 16 ships ``opshub workspace generate``, which regenerates the
disposable markdown workspace (ADR-0003) from the ``tasks`` projection.

Phase 3 step C3 adds ``opshub workspace ingest`` (with an optional
``--dry-run`` flag), which scans ``<workspace.root>/inbox/*.md`` and
forwards new files into the event log via
:class:`opshub.services.file_ingest_service.FileIngestService`
(idempotent on content hash — see PR #53 / phase-3-plan §3 機能 §5).

Heavy imports (``opshub.markdown``, ``opshub.core.config``,
``opshub.db``, ``opshub.services``, ``opshub.projections``) are deferred
to call time so that ``opshub --help`` cold start stays under the
ADR-0001 ~300ms budget; the module-level surface is limited to Typer and
``__future__``.
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


@workspace_app.command("ingest")
def workspace_ingest(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Scan only; report which files would be enqueued without writing events.",
    ),
) -> None:
    """Ingest new ``workspace/inbox/*.md`` files into the event log.

    Reads each ``.md`` file under ``<workspace.root>/inbox/``
    (immediate children only — sub-directories are deliberately out of
    scope per the C2 contract), parses its summary + optional
    ``source_ref`` via :mod:`opshub.markdown.ingest`, and emits an
    :class:`ItemEnqueued` + :class:`FileIngested` event pair for any
    file whose ``content_hash`` is not already in the
    ``ingested_files`` projection. Idempotent across runs: re-running
    on unchanged files writes nothing.

    With ``--dry-run`` the command scans the inbox directory and prints
    which files would be enqueued vs skipped, but appends no events and
    updates no projections — the projection is read but never mutated.
    """
    # Lazy imports keep ``opshub --help`` cold start under the
    # ADR-0001 ~300ms budget (M6 import-guard test enforces this).
    from opshub.core.config import OpsHubSettings

    settings = OpsHubSettings()
    workspace_root = settings.workspace.root

    if dry_run:
        # ``--dry-run`` deliberately avoids constructing
        # :class:`FileIngestService` so no event store / projector is
        # wired up — the contract is "scan only, write nothing". We
        # read the ``ingested_files`` projection directly to classify
        # each file as "would enqueue" vs "already ingested".
        from sqlalchemy import select

        from opshub.cli._wiring import build_engine
        from opshub.markdown.ingest import compute_file_hash
        from opshub.projections.ingested_files import ingested_files_table

        inbox_dir = workspace_root / "inbox"
        if not inbox_dir.is_dir():
            typer.echo(f"no inbox dir at {inbox_dir}")
            return
        engine = build_engine()
        try:
            with engine.connect() as conn:
                known = {
                    row[0]
                    for row in conn.execute(select(ingested_files_table.c.content_hash)).all()
                }
        finally:
            engine.dispose()
        would_enqueue: list[str] = []
        would_skip: list[str] = []
        # ``glob("*.md")`` (not ``rglob``) mirrors the C2 service
        # contract — sub-directories are silently skipped.
        for path in sorted(inbox_dir.glob("*.md")):
            if not path.is_file():
                continue
            content_hash = compute_file_hash(path)
            try:
                rel = str(path.relative_to(workspace_root))
            except ValueError:
                rel = str(path)
            if content_hash in known:
                would_skip.append(rel)
            else:
                would_enqueue.append(rel)
        typer.echo(
            f"would enqueue {len(would_enqueue)} file(s), skip {len(would_skip)} (already ingested)"
        )
        for p in would_enqueue:
            typer.echo(f"  + {p}")
        for p in would_skip:
            typer.echo(f"  . {p}")
        return

    from opshub.cli._wiring import build_file_ingest_service

    service = build_file_ingest_service(actor="cli:workspace_ingest")
    result = service.ingest_inbox_dir(workspace_root)
    typer.echo(
        f"enqueued {result.enqueued_count} item(s), "
        f"skipped {result.skipped_count} (already ingested)"
    )
    for path in result.enqueued_paths:
        try:
            rel = str(path.relative_to(workspace_root))
        except ValueError:
            rel = str(path)
        typer.echo(f"  + {rel}")
