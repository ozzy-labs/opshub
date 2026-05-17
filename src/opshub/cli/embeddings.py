"""``opshub embeddings ...`` subcommands.

Phase 4 step B3 implements the two CLI surfaces ADR-0012 §6 specifies for
the CLI-driven rebuild flow:

* ``opshub embeddings rebuild [--entity-type X] [--limit N]`` — calls
  :meth:`EmbeddingService.embed_pending` (Phase 4 step B2 / PR #69) and
  prints the embedded / skipped / failed counts plus the
  ``rebuild_run_id`` of the bracketing
  :class:`~opshub.domain.events.embedding.EmbeddingRebuildRequested`
  event. The rebuild is idempotent: a second run on the same data is a
  no-op until the configured backend / model changes.
* ``opshub embeddings status [--format table|json|md]`` — shows the
  active backend, the resolved ``model_id`` / ``model_version`` of the
  configured embedder, and a per-entity-type breakdown of total /
  embedded / pending counts. When the backend is ``"disabled"`` the
  status prints a one-line hint and exits 0 without scanning the
  database (so an operator with no DB initialised yet still gets a
  usable answer).

This module replaces the Phase 1 placeholder that only knew how to
report ``backend=disabled / embeddings: 0 rows``. The Phase 1 placeholder
hard-coded ``OPSHUB_EMBEDDING__BACKEND=disabled`` semantics; the Phase 4
implementation talks to the real
:class:`~opshub.services.embedding_service.EmbeddingService` +
``embeddings`` projection.

Module-level imports stay limited to ``__future__`` plus ``typer`` so
``opshub --help`` cold start stays under the ADR-0001 budget. The
``test_cli_imports`` static check enforces this on every CI run; all the
heavy paths (SQLAlchemy, embedder factories, the projector pipeline)
load lazily inside the command callbacks.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

embeddings_app = typer.Typer(
    name="embeddings",
    help="Embedding operations.",
    no_args_is_help=True,
)


@embeddings_app.command("rebuild")
def embeddings_rebuild(
    entity_type: str | None = typer.Option(
        None,
        "--entity-type",
        "-t",
        help="Restrict rebuild to one entity type: task / decision / inbox_item / source.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="Cap the number of entities to embed in this run.",
    ),
) -> None:
    """Embed every entity that lacks a current (model_id, model_version) embedding.

    Idempotent: re-running on the same data is a no-op until the
    configured backend / model is changed. The ``rebuild_run_id``
    surfaces the ULID of the bracketing
    :class:`~opshub.domain.events.embedding.EmbeddingRebuildRequested`
    event so the run can be correlated with downstream logs.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_embedding_service

    service = build_embedding_service()
    result = service.embed_pending(entity_type=entity_type, limit=limit)
    typer.echo(
        f"rebuild_run_id={result.rebuild_run_id}: "
        f"embedded {result.embedded_count}, "
        f"skipped {result.skipped_count}, "
        f"failed {result.failed_count}"
    )


@embeddings_app.command("status")
def embeddings_status(
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table | json | md.",
    ),
) -> None:
    """Show embedding backend, model identity, and per-entity-type counts.

    With ``backend=disabled`` the command short-circuits before touching
    the database: an operator who has not yet run ``opshub init`` still
    sees a clear "embedding is off" hint. For any active backend the
    command opens the engine, resolves the configured embedder via
    :mod:`opshub.vectors.factory` (so ``model_id`` / ``model_version``
    in the output match what ``embeddings rebuild`` will write), and
    counts ``embeddings`` rows scoped to that ``(model_id,
    model_version)`` against the per-entity-type projection tables.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from sqlalchemy import func, select, text

    from opshub.cli._render import Column, dispatch
    from opshub.cli._wiring import build_engine
    from opshub.core.config import OpsHubSettings
    from opshub.projections.decisions import decisions_table
    from opshub.projections.inbox import inbox_items_table
    from opshub.projections.sources import sources_table
    from opshub.projections.tasks import tasks_table

    settings = OpsHubSettings()
    backend = settings.embedding.backend

    if backend == "disabled":
        # Fast path: no need to open the engine. Operators running
        # `embeddings status` immediately after install (no `opshub init`
        # yet) still get an actionable answer instead of a ConfigError.
        typer.echo("backend=disabled")
        typer.echo(
            "(no embeddings; set [embedding] backend to local/openai/voyage "
            "and run `opshub embeddings rebuild`)"
        )
        return

    # Resolve the configured embedder so the status mirrors what
    # `embeddings rebuild` would write. The factory branches lazily into
    # the concrete embedder module (no extras imported for the
    # not-selected backends).
    from opshub.vectors.factory import build_embedder

    embedder = build_embedder(settings)
    model_id = embedder.model_id
    model_version = embedder.model_version

    typer.echo(f"backend={backend}")
    typer.echo(f"model_id={model_id} version={model_version}")

    # Per-entity-type counts: total comes from the projection table,
    # embedded comes from the `embeddings` metadata table filtered by
    # the current (model_id, model_version). The `embeddings` table is
    # not registered on the shared metadata (matches the SqliteVecStore
    # precedent in PR #67), so we count it via raw text() SQL with bind
    # params -- the same shape EmbeddingService._iter_pending uses.
    entity_tables = {
        "task": tasks_table,
        "decision": decisions_table,
        "inbox_item": inbox_items_table,
        "source": sources_table,
    }
    engine = build_engine()
    rows: list[dict[str, object]] = []
    with engine.connect() as conn:
        for entity_type, table in entity_tables.items():
            total = conn.execute(select(func.count()).select_from(table)).scalar_one()
            embedded = conn.execute(
                text(
                    "SELECT COUNT(*) FROM embeddings "
                    "WHERE entity_type = :et "
                    "  AND model_id = :mid "
                    "  AND model_version = :mv"
                ).bindparams(et=entity_type, mid=model_id, mv=model_version),
            ).scalar_one()
            rows.append(
                {
                    "entity_type": entity_type,
                    "total": int(total),
                    "embedded": int(embedded),
                    "pending": int(total) - int(embedded),
                }
            )

    columns = [
        Column("entity_type", lambda r: r["entity_type"]),
        Column("total", lambda r: r["total"], md_align="right"),
        Column("embedded", lambda r: r["embedded"], md_align="right"),
        Column("pending", lambda r: r["pending"], md_align="right"),
    ]
    typer.echo(dispatch(fmt, columns, rows))
