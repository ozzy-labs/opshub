"""``opshub connector ...`` subcommands.

Phase 3 step A5 ships two connector commands:

* ``opshub connector list`` — prints every connector currently in the
  registry, one name per line. With no concrete connectors registered
  (the Phase 3 MVP state before sub-issue B lands), prints
  ``no connectors registered`` and exits 0 — a healthy "framework is
  wired, no connectors yet" report.
* ``opshub connector sync <name>`` — resolves ``<name>`` from the
  registry, loads the cursor, opens a sync run bracket via
  :meth:`SourceService.cursor_set` with ``sync_started=True``, invokes
  the connector, persists the returned cursor, and prints a one-line
  summary. Failures are sanitised (only the exception type name is
  surfaced) to avoid leaking secrets / PII into the event log.

Module-level imports are restricted to ``__future__`` and ``typer`` so
``opshub --help`` cold start stays under the ~300ms budget set by
ADR-0001; everything heavy (the connectors package, ``_wiring``,
``core.logging``, :class:`ConnectorContext`) is imported lazily inside
each command callback. The static check in
``tests/integration/test_cli_imports.py`` enforces this whitelist on
every CI run.
"""

from __future__ import annotations

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

connector_app = typer.Typer(
    name="connector",
    help="External SaaS connectors.",
    no_args_is_help=True,
)

# ``auth`` is a nested Typer sub-app so the surface remains
# ``opshub connector auth set <name>`` (mirrors gh / aws CLI patterns).
# Constructing the sub-app at module level is cheap — it is just a Typer
# instance, no heavy imports — so the ADR-0001 cold-start budget is
# preserved (the ``test_cli_imports`` static check covers this file).
auth_app = typer.Typer(
    name="auth",
    help="Connector authentication.",
    no_args_is_help=True,
)
connector_app.add_typer(auth_app)


@connector_app.command("list")
def connector_list() -> None:
    """List every registered connector, one name per line.

    Empty registry is the normal Phase 3 MVP state before sub-issue B
    lands; prints ``no connectors registered`` and exits 0.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.connectors import discover_connectors

    connectors = discover_connectors()
    if not connectors:
        typer.echo("no connectors registered")
        return
    for connector in connectors:
        typer.echo(connector.name)


@connector_app.command("sync")
def connector_sync(name: str) -> None:
    """Run sync for the named connector.

    Resolves ``name`` from :func:`discover_connectors`, loads the
    cursor from the ``connector_cursors`` projection, opens a sync run
    bracket (``cursor_set(sync_started=True)``), invokes
    :meth:`Connector.sync`, persists the returned cursor
    (``cursor_set(sync_started=False, value=result.new_cursor)``), and
    prints a one-line summary.

    On exception:

    * ``SourceService.record_sync_failure`` records a
      ``ConnectorSyncFailed`` event with the exception **type name only**
      — never the message — so secrets / PII never reach the event log.
    * The CLI exits with code 1.

    Unknown connector → exit code 2 with a list of available names
    (mirrors Typer's convention for usage errors).
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001) and satisfy the
    # ``test_cli_imports`` static check.
    from typing import Any

    # Importing the github subpackage triggers
    # ``register_connector(GitHubConnector())`` as an import side
    # effect (see ``opshub.connectors.github.__init__``). Phase 3.x
    # will replace this with entry-points / scan-based discovery; for
    # the MVP an explicit import per connector is honest and easy to
    # audit, and other connectors (Slack / MS365 / Box) will add their
    # own lines alongside.
    import opshub.connectors.github  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from opshub.connectors import discover_connectors
    from opshub.connectors.context import ConnectorContext
    from opshub.core.logging import get_logger

    connectors = {c.name: c for c in discover_connectors()}
    connector = connectors.get(name)
    if connector is None:
        available = ", ".join(connectors) or "(none)"
        typer.echo(
            f"unknown connector {name!r}; available: {available}",
            err=True,
        )
        raise typer.Exit(code=2)

    # NOTE: ``build_source_service`` lands in step A4 (running in parallel
    # with this PR). Until A4 merges, this lazy import fails at runtime —
    # which is fine because the only path that reaches it requires a
    # registered concrete connector (added by sub-issue B). The list-only
    # and unknown-connector tests never execute past the early return
    # above. pyright cannot see A4's wiring yet, so the source service is
    # typed as :class:`~typing.Any` to keep this module typecheck-clean
    # while A4/A5/A6 race; the public ``SourceService`` type binds when
    # A4 merges.
    source: Any = _build_source_service(actor=f"connector:{name}")
    logger = get_logger().bind(connector=name)
    cursor = source.cursor_get(name)
    # Open the sync run bracket so observers see ConnectorSyncStarted.
    source.cursor_set(name, cursor, sync_started=True)
    context = ConnectorContext(
        source_service=source,
        cursor_value=cursor,
        secrets=None,  # refined in B1 once core.secrets lands
        logger=logger,
    )
    try:
        result = connector.sync(context)
    except Exception as exc:
        # Sanitise: surface only the exception type name, never the
        # message, so tokens / PII never reach the event log.
        source.record_sync_failure(name, error_message=type(exc).__name__)
        typer.echo(f"sync failed: {type(exc).__name__}", err=True)
        raise typer.Exit(code=1) from exc

    source.cursor_set(name, result.new_cursor, sync_started=False)
    typer.echo(f"synced {name}: {result.observed_count} item(s) observed")


@auth_app.command("set")
def auth_set(
    name: str = typer.Argument(..., help="Connector name (e.g. 'github')."),
    token: str | None = typer.Option(
        None,
        "--token",
        help="Token value. If omitted, read securely from stdin (hidden input).",
    ),
) -> None:
    """Store a credential token for a connector in the OS keychain.

    Per ADR-0014, tokens are stored via ``keyring`` (OS-backed: macOS
    Keychain / Linux Secret Service / Windows Credential Locker). Use
    the ``OPSHUB_CONNECTOR_<NAME>_PAT`` env var to override at runtime
    without touching the keychain (useful for CI / containers).

    Currently supported names: ``github``, ``embedder:openai``,
    ``embedder:voyage``, ``llm:anthropic``, ``llm:openai``. Other
    connectors (Slack / MS365 / Box) land in Phase 3.x; additional LLM
    / embedder backends would extend this switch alongside their
    factory branch in :mod:`opshub.llm.factory` /
    :mod:`opshub.vectors.factory`.

    Security: there is intentionally no ``auth get`` command — we never
    echo tokens to stdout. The env-var override is the documented
    escape hatch for testing / debugging.
    """
    # Lazy imports keep CLI cold start fast (ADR-0001) and keep this
    # module compatible with ``tests/integration/test_cli_imports``.
    from opshub.core.secrets import set_secret

    if name == "github":
        from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY

        key = GITHUB_PAT_SECRET_KEY
    elif name == "embedder:openai":
        # The embedder modules export the keyring key as a module
        # constant so the CLI writer + embedder reader cannot drift.
        # Lazy-imported per-branch so the OpenAI / Voyage SDK paths
        # remain off the cold-start path when the operator never uses
        # them.
        from opshub.vectors.openai_embedder import OPENAI_API_KEY_SECRET

        key = OPENAI_API_KEY_SECRET
    elif name == "embedder:voyage":
        from opshub.vectors.voyage_embedder import VOYAGE_API_KEY_SECRET

        key = VOYAGE_API_KEY_SECRET
    elif name == "llm:anthropic":
        # Phase 5 step A5: LLM client modules export the keyring key as
        # a module constant so the CLI writer + client reader cannot
        # drift (mirrors the embedder pattern above). Lazy-imported so
        # the ``anthropic`` SDK does not load on the ``opshub --help``
        # cold-start path when the operator never uses LLM features.
        from opshub.llm.anthropic_client import ANTHROPIC_API_KEY_SECRET

        key = ANTHROPIC_API_KEY_SECRET
    elif name == "llm:openai":
        from opshub.llm.openai_client import OPENAI_API_KEY_SECRET as LLM_OPENAI_API_KEY_SECRET

        key = LLM_OPENAI_API_KEY_SECRET
    else:
        typer.echo(
            f"unknown auth target {name!r}; currently supported: "
            "github, embedder:openai, embedder:voyage, "
            "llm:anthropic, llm:openai",
            err=True,
        )
        raise typer.Exit(code=2)

    if token is None:
        # Securely prompt without echoing the token to the terminal.
        # ``typer.prompt`` returns ``str`` for the default ``type=str``
        # case but its public signature is loosely typed (``Any``); we
        # bind the result to a freshly-named ``str`` to keep pyright /
        # mypy happy without an outright cast.
        raw: str = typer.prompt("Token", hide_input=True)
    else:
        raw = token

    stripped = raw.strip()
    if not stripped:
        typer.echo("token must be non-empty", err=True)
        raise typer.Exit(code=2)

    set_secret(key, stripped)
    typer.echo(f"stored token for connector {name!r}")


def _build_source_service(*, actor: str) -> object:
    """Indirection for the step-A4 ``build_source_service`` helper.

    Phase 3 step A4 adds ``opshub.cli._wiring.build_source_service``;
    until that PR merges, importing it eagerly inside :func:`connector_sync`
    would force pyright to flag the unknown attribute on every CI run.
    Hiding the lookup behind this private helper (returning ``object``
    so the caller can cast to ``Any`` for the still-unknown
    :class:`SourceService` interface) keeps the type checker green
    during the A4/A5/A6 race; when A4 lands the indirection can be
    inlined.
    """
    from opshub.cli import _wiring  # local import preserves cold-start budget

    builder = getattr(_wiring, "build_source_service", None)
    if builder is None:
        raise RuntimeError(
            "opshub.cli._wiring.build_source_service is not available; "
            "Phase 3 step A4 must merge before `opshub connector sync` "
            "can run."
        )
    return builder(actor=actor)
