"""``opshub search`` — full-text search across the body store.

Phase 10 step B2 (Sub-issue B, ADR-0012 改訂版 §4 + ADR-0020) ships
the FTS5-backed surface that complements semantic ``opshub recall``.
Vector recall finds paraphrase / semantic neighbours; ``search``
finds exact tokens (function names, channel IDs, URLs, ticket
strings) that the embedder would miss. Together they form the hybrid
search the Phase 10 Plan §3-B + Sub-issue B DoD call for.

Shape rationale
---------------

Like :mod:`opshub.cli.recall`, ``search`` has no sub-verbs — the
query goes straight to the top-level command via the same
``Typer.command`` registration pattern. Typer 0.25 + Click 8 force a
sub-Typer into a Click group expecting ``COMMAND [ARGS]...`` after
group options, which would break ``opshub search "query" --format
json``. Registering as a flat command sidesteps the issue while
keeping the registration site symmetric with ``recall``.

Module-level imports stay limited to ``__future__`` plus ``typer`` so
``opshub --help`` cold start stays under the ADR-0001 budget. The
``test_cli_imports`` static check enforces this on every CI run; all
heavy paths load lazily inside the command body.

Exit-code contract
------------------

* :class:`~opshub.core.errors.ConfigError` raised by
  :class:`~opshub.services.search_service.SearchService` (empty query)
  → exit 2 with the service-supplied message. Rendering the message
  before re-raising keeps the UX consistent with the other CLI
  commands that handle their own ConfigError surface.
* No hits → exit 0 + a one-line ``no hits for '<query>'`` message
  (zero results is not an error; just means nothing in the body
  store matched).
* Otherwise → exit 0 + the formatted hit table on stdout.

No backend-disabled short circuit
---------------------------------

Unlike :mod:`opshub.cli.recall`, FTS5 has no backend selection — the
index is built into SQLite and populated automatically by the
:class:`opshub.projections.sources.SourcesProjection` reducer. The
operator doesn't need to configure anything for ``opshub search`` to
work; if there are no body rows yet, the search returns zero hits.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside the command body (ADR-0001 lazy-import rule).


def search_command(
    query: str = typer.Argument(..., help="Free-form query text."),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        help="Maximum number of hits returned.",
    ),
    connector: str | None = typer.Option(
        None,
        "--connector",
        "-c",
        help=(
            "Restrict to hits from one connector, matched against the "
            "source's connector name (e.g. github, slack, web)."
        ),
    ),
    raw_query: bool = typer.Option(
        False,
        "--raw",
        help=(
            "Power-user: pass the query straight to FTS5 (boolean / "
            "phrase / prefix syntax, e.g. 'box* AND 権限*'). The default "
            "non-raw mode already handles Japanese substring matches "
            "via the trigram tokenizer (Phase 15, ADR-0028); 1-2 "
            "character queries fall back to a body LIKE scan. --raw "
            "disables that fallback so 1-2 char inputs may return 0 "
            "hits."
        ),
    ),
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table | json | md.",
    ),
) -> None:
    """Run a full-text search over the source body store.

    The query is sent to the ``sources_fts`` FTS5 virtual table
    (created by migration ``0019``) and joined back to ``sources``
    for metadata. See
    :class:`opshub.services.search_service.SearchService` for the
    full pipeline.
    """
    # Lazy imports keep CLI cold start fast (ADR-0001) and satisfy the
    # ``test_cli_imports`` module-level whitelist.
    from opshub.cli._render import Column, dispatch, id_prefix
    from opshub.cli._wiring import build_search_service
    from opshub.core.errors import ConfigError

    service = build_search_service()
    try:
        hits = service.search(
            query,
            limit=limit,
            connector_name=connector,
            raw_query=raw_query,
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if not hits:
        typer.echo(f"no hits for {query!r}")
        return

    columns = [
        Column("score", lambda h: f"{h.score:.3f}", md_align="right"),
        Column("connector", lambda h: h.connector_name),
        Column("entity_id", lambda h: id_prefix(h.entity_id)),
        Column("title", lambda h: h.title),
    ]
    typer.echo(dispatch(fmt, columns, hits))


def register(app: typer.Typer) -> None:
    """Register :func:`search_command` on the root Typer app.

    Mirrors :func:`opshub.cli.recall.register` — the command's name
    (``"search"``) and short help stay co-located with the body, and
    the registration site in ``cli/app.py`` reads symmetric with
    :func:`opshub.cli.recall.register`.
    """
    app.command(
        name="search",
        help="Full-text search across source body content.",
    )(search_command)
