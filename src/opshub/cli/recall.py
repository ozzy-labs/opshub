"""``opshub recall`` — semantic search across opshub entities.

Phase 4 step C2 (ADR-0012 §7) ships the primary user-facing surface for
the Phase 4 MVP: ``opshub recall "<query>"`` runs a hybrid semantic
search (vector recall + SQL filter + metadata JOIN) against the active
embedding backend and prints the top-N hits.

Shape rationale
---------------

Unlike ``opshub task list`` / ``opshub embeddings rebuild``, ``recall``
has no list / show / delete sub-verbs — the query goes directly to the
top-level command. With Typer 0.25 + Click 8, a ``Typer`` sub-app
registered via :meth:`Typer.add_typer` is always materialised as a
Click *group* that expects ``COMMAND [ARGS]...`` after the group's
options, even when the group has a single
``@callback(invoke_without_command=True)`` callback. That breaks the
ergonomic ``opshub recall "query" --format json`` invocation, because
``--format`` is then parsed as a (non-existent) sub-command. The
cleaner pattern for "no sub-verb" in Typer is to register the command
as a flat :meth:`Typer.command` on the root app. This module exposes
:func:`recall_command` (the body) plus a tiny :func:`register` helper
that :mod:`opshub.cli.app` calls during command registration — the
top-level "looks like a sub-app" shape is preserved at the import +
registration site without the Click-group baggage.

Module-level imports stay limited to ``__future__`` plus ``typer`` so
``opshub --help`` cold start stays under the ADR-0001 budget. The
``test_cli_imports`` static check enforces this on every CI run; the
heavy paths (settings, wiring, error types, renderer helpers) load
lazily inside the command body.

Exit-code contract
------------------

* ``backend=disabled`` → exit 2 + stderr setup hint. The check fires
  before :func:`build_recall_service` so an operator running the
  command straight after install (no engine open, no embedder
  resolved) gets a clean error rather than a stack trace.
* :class:`~opshub.core.errors.ConfigError` raised by
  :class:`~opshub.services.recall_service.RecallService` (missing
  embeddings for the active model, unsupported state filter, empty
  query) → exit 2 with the service-supplied message. Rendering the
  message before re-raising keeps the UX consistent with the other
  CLI commands that handle their own ConfigError surface.
* No hits → exit 0 + a one-line ``no hits for '<query>'`` message
  (zero results is not an error; it just means the corpus has
  nothing similar).
* Otherwise → exit 0 + the formatted hit table on stdout.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside the command body (ADR-0001 lazy-import rule).


def recall_command(
    query: str = typer.Argument(..., help="Free-form query text."),
    entity_type: str | None = typer.Option(
        None,
        "--type",
        "-t",
        help="Restrict results to one entity type: task / decision / inbox_item / source.",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        help="Maximum number of hits returned.",
    ),
    state: str | None = typer.Option(
        None,
        "--state",
        "-s",
        help="Filter hits to entities with this state (task / inbox_item only).",
    ),
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table | json | md.",
    ),
) -> None:
    """Run semantic search and print the top-N hits.

    The query is embedded via the active backend, sent to the vector
    store for nearest-neighbour recall, and joined with the per-entity
    projection tables to attach human-readable titles. See
    :class:`opshub.services.recall_service.RecallService` for the full
    pipeline.
    """
    # Lazy imports keep CLI cold start fast (ADR-0001) and satisfy the
    # ``test_cli_imports`` module-level whitelist.
    from opshub.cli._render import Column, dispatch, id_prefix
    from opshub.cli._wiring import build_recall_service
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError

    settings = OpsHubSettings()
    if settings.embedding.backend == "disabled":
        # Short-circuit BEFORE :func:`build_recall_service` so we never
        # open the SQLite engine / resolve an embedder when the operator
        # has not turned a backend on. The hint mirrors the message
        # ``embeddings status`` emits on the same path.
        typer.echo(
            "Embedding backend is disabled. Set [embedding] backend to "
            "'local' / 'openai' / 'voyage' in ~/.config/opshub/config.toml "
            "and run `opshub embeddings rebuild`.",
            err=True,
        )
        raise typer.Exit(code=2)

    service = build_recall_service()
    try:
        hits = service.recall(
            query,
            entity_type=entity_type,
            limit=limit,
            state=state,
        )
    except ConfigError as exc:
        # :class:`RecallService` surfaces missing-embedding /
        # unsupported-filter combinations as :class:`ConfigError`. The
        # top-level :func:`opshub.cli.app.main` handler maps
        # :class:`OpsHubError` → exit code 1, but recall-specific
        # ConfigErrors are usage problems (operator forgot to run
        # ``embeddings rebuild`` / picked an invalid state filter), so
        # we render the error and exit 2 to match the
        # ``backend=disabled`` shape above.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if not hits:
        typer.echo(f"no hits for {query!r}")
        return

    columns = [
        Column("score", lambda h: f"{h.score:.3f}", md_align="right"),
        Column("entity_type", lambda h: h.entity_type),
        Column("entity_id", lambda h: id_prefix(h.entity_id)),
        Column("title", lambda h: h.title),
    ]
    typer.echo(dispatch(fmt, columns, hits))


def register(app: typer.Typer) -> None:
    """Register :func:`recall_command` on the root Typer app.

    Encapsulates the registration knob so :mod:`opshub.cli.app` only has
    to call ``register(app)`` — the command's name (``"recall"``) and
    short help stay co-located with the body. Mirrors the
    ``add_typer`` shape used by every other sub-command module so the
    registration block in ``cli/app.py`` reads uniformly.
    """
    app.command(
        name="recall",
        help="Semantic search across opshub entities.",
    )(recall_command)
