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
    purge: bool = typer.Option(
        False,
        "--purge",
        help=(
            "Drop existing embeddings for the scope before re-embedding. "
            "Use this after Phase 10 step B2 (body-based embedding, "
            "ADR-0012 改訂版 §4) to force re-embed from sources.body."
        ),
    ),
) -> None:
    """Embed every entity that lacks a current (model_id, model_version) embedding.

    Idempotent by default: re-running on the same data is a no-op
    until the configured backend / model is changed. The
    ``rebuild_run_id`` surfaces the ULID of the bracketing
    :class:`~opshub.domain.events.embedding.EmbeddingRebuildRequested`
    event so the run can be correlated with downstream logs.

    ``--purge`` (Phase 10 step B2): drops the existing
    ``(model_id, model_version)`` embeddings for the scope before the
    rebuild kicks in. Use this when the embed input shape changed but
    the model identity did not — the canonical case is migrating from
    ``sources.summary`` to ``COALESCE(sources.body, sources.summary)``
    (ADR-0012 改訂版 §4). Without ``--purge`` the rebuild's
    ``NOT EXISTS`` filter sees the entity as "already embedded" and
    keeps the stale summary-based vector.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_embedding_service

    service = build_embedding_service()
    purged = 0
    if purge:
        purged = service.purge_embeddings(entity_type=entity_type)
    result = service.embed_pending(entity_type=entity_type, limit=limit)
    if purge:
        typer.echo(f"purged {purged} existing embedding(s) before rebuild")
    typer.echo(
        f"rebuild_run_id={result.rebuild_run_id}: "
        f"embedded {result.embedded_count}, "
        f"skipped {result.skipped_count}, "
        f"failed {result.failed_count}"
    )


@embeddings_app.command("drain")
def embeddings_drain(
    entity_type: str | None = typer.Option(
        None,
        "--entity-type",
        "-t",
        help="Restrict drain to one entity type: task / decision / inbox_item / source.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="Cap the number of entities processed in this run.",
    ),
) -> None:
    """Drain pending embeddings (catch up entities that auto-embed missed).

    Thin wrapper around :meth:`EmbeddingService.embed_pending`
    intended for the Phase 5 step C1 auto-embed path: when
    ``[embedding] auto = true`` is configured but some hooks failed
    silently (e.g. the embedder API was transiently rate-limited),
    running ``drain`` is the explicit retry surface that picks up
    every entity still in the "pending" state via the same
    ``NOT EXISTS`` predicate the rebuild flow uses.

    With ``auto = false`` this command is functionally equivalent to
    ``opshub embeddings rebuild`` — both call :meth:`embed_pending`
    and respect the same ``--entity-type`` / ``--limit`` flags — but
    is more intent-explicit for the "auto missed something" use case.
    The bracketing :class:`EmbeddingRebuildRequested` event is still
    appended so audit trails treat drains and rebuilds uniformly.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_embedding_service

    service = build_embedding_service(actor="cli:embeddings_drain")
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

    Phase 5 step C2 extends the output with an ``auto:`` diagnostic line
    reporting whether ``[embedding] auto`` is enabled. When ``auto =
    true`` but ``backend = disabled`` the operator gets a one-line
    warning so the misconfiguration surfaces during the most common
    debugging entry point (running ``embeddings status``).
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
    from opshub.services.auto_embed_hook import AUTO_EMBED_EVENT_TYPES

    settings = OpsHubSettings()
    backend = settings.embedding.backend
    auto = settings.embedding.auto

    if backend == "disabled":
        # Fast path: no need to open the engine. Operators running
        # `embeddings status` immediately after install (no `opshub init`
        # yet) still get an actionable answer instead of a ConfigError.
        typer.echo("backend=disabled")
        typer.echo(
            "(no embeddings; set [embedding] backend to local/openai/voyage "
            "and run `opshub embeddings rebuild`)"
        )
        if auto:
            # The auto flag is wired into the composition root so the
            # hook short-circuits when backend=disabled (see
            # :func:`opshub.cli._wiring._maybe_build_auto_embed_hooks`).
            # Surface the conflict explicitly here so the operator does
            # not assume their auto-embed setting is taking effect.
            typer.echo(
                "auto: enabled but [embedding] backend = disabled "
                "(auto hook will skip; configure backend or set auto = false)"
            )
        else:
            typer.echo("auto: disabled")
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
    if auto:
        # Backend is active here, so the auto-embed hook is registered
        # by :func:`_maybe_build_auto_embed_hooks` for every service
        # that emits embeddable events. Echo the active event set so
        # the operator can verify which event types trigger the hook
        # without having to grep the source.
        event_list = ", ".join(sorted(AUTO_EMBED_EVENT_TYPES))
        typer.echo("auto: enabled")
        typer.echo(f"auto-embed hook: active for events {{{event_list}}}")
    else:
        typer.echo("auto: disabled")

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


@embeddings_app.command("find-duplicates")
def embeddings_find_duplicates(
    threshold: float = typer.Option(
        0.92,
        "--threshold",
        "-t",
        help="Minimum cosine similarity (0..1) to emit as a duplicate pair.",
    ),
    entity_type: str = typer.Option(
        "source",
        "--entity-type",
        "-e",
        help="Entity family to scan: task / decision / inbox_item / source.",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        "-n",
        help="Cap on returned pairs (sorted highest-similarity first).",
    ),
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table / json / md.",
    ),
) -> None:
    """Find near-duplicate pairs above the similarity threshold (Phase 4 step C3).

    Scans the active backend's embeddings for entity pairs whose
    cosine similarity exceeds ``--threshold``. Self-matches and
    reverse-pairs are de-duplicated; results are sorted highest
    similarity first. See :class:`opshub.services.DuplicateService`
    for the conversion from sqlite-vec L2 distance to cosine
    similarity (the formula assumes unit-normalised vectors, which
    every supported backend produces — ADR-0012 §1).
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._render import Column, dispatch, id_prefix, truncate
    from opshub.cli._wiring import build_duplicate_service

    service = build_duplicate_service()
    pairs = service.find_duplicates(
        entity_type=entity_type,
        threshold=threshold,
        limit=limit,
    )
    if not pairs:
        typer.echo(f"no duplicates above {threshold:.2f} (entity_type={entity_type})")
        return

    columns = [
        Column(
            "Entity A",
            lambda p: f"{id_prefix(p.entity_id_a)}: {truncate(p.text_a, 40)}",
            width=52,
        ),
        Column(
            "Entity B",
            lambda p: f"{id_prefix(p.entity_id_b)}: {truncate(p.text_b, 40)}",
            width=52,
        ),
        Column(
            "Similarity",
            lambda p: f"{p.similarity:.3f}",
            width=10,
            md_align="right",
        ),
    ]
    typer.echo(dispatch(fmt, columns, pairs))
