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

from typing import TYPE_CHECKING

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

if TYPE_CHECKING:
    from collections.abc import Callable

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

    # Importing each connector subpackage triggers ``register_connector``
    # as an import side effect (see ``opshub.connectors.<name>.__init__``).
    # Phase 3.x will replace this with entry-points / scan-based
    # discovery; for the MVP an explicit import per connector is honest
    # and easy to audit. ``ImportError`` is swallowed for every connector
    # so an operator who installed only a subset of the
    # ``[connectors-github]`` / ``[connectors-slack]`` / ``[connectors-ms365]`` /
    # ``[connectors-box]`` extras can still sync the connector they *did*
    # install — the import only fails when the extras-bundled SDK is
    # missing AND the connector module touches it at import time. Each
    # connector package is import-clean (heavy SDKs are deferred into
    # method bodies), so these guards are defensive: they keep a future
    # refactor that adds a top-level SDK import from breaking sync for the
    # *other* connectors (the regression this guards against bit GitHub
    # before its ``httpx`` import was deferred — see
    # ``opshub.connectors.github``).
    try:
        import opshub.connectors.github  # pyright: ignore[reportUnusedImport]
    except ImportError:
        # GitHub connector module imports cleanly without the extras (the
        # heavy ``httpx`` import is deferred into ``GitHubConnector.sync``);
        # this branch is defensive and would only trigger if a future
        # refactor re-introduces a top-level SDK import.
        pass

    try:
        import opshub.connectors.slack  # pyright: ignore[reportUnusedImport]
    except ImportError:
        # Slack connector module imports cleanly without the extras
        # (the heavy ``slack_sdk`` imports stay inside the auth /
        # fetcher methods); this branch is defensive and would only
        # trigger if a future refactor adds a top-level SDK import.
        pass

    try:
        import opshub.connectors.ms365  # pyright: ignore[reportUnusedImport]
    except ImportError:
        # MS365 connector module imports cleanly without the extras (the
        # heavy ``msal`` / ``httpx`` imports stay inside the auth /
        # fetcher constructors); this branch is defensive and would only
        # trigger if a future refactor adds a top-level SDK import.
        pass

    try:
        import opshub.connectors.box  # pyright: ignore[reportUnusedImport]
    except ImportError:
        # Box connector module imports cleanly without the extras (the
        # heavy ``boxsdk`` imports stay inside the auth / fetcher
        # constructors); this branch is defensive and would only trigger
        # if a future refactor adds a top-level SDK import.
        pass

    try:
        import opshub.connectors.box_drive  # pyright: ignore[reportUnusedImport]
    except ImportError:
        # Box Drive connector module imports cleanly with no third-party
        # extras (Phase 9, ADR-0019 — the scanner is pure stdlib
        # ``os.scandir``). This guard is defensive and would only fire
        # if a future refactor adds a heavy top-level dependency.
        pass

    try:
        import opshub.connectors.teams  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        # Teams connector module imports cleanly without the extras
        # (the heavy ``httpx`` imports stay inside the fetcher
        # constructor); this branch is defensive and would only
        # trigger if a future refactor adds a top-level SDK import.
        pass
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

    Currently supported names: ``github``, ``connector:slack`` (or
    legacy ``slack``), ``embedder:openai``, ``embedder:voyage``,
    ``llm:anthropic``, ``llm:openai``, ``connector:ms365``,
    ``connector:box``. The recommended form for the Phase 7 connectors
    is the ``connector:<name>`` namespace (matches keyring key prefix
    ``connector:<name>:<purpose>`` and the Phase 7 plan §1 #4 contract);
    the bare ``slack`` form is a backward-compat alias retained for
    operator scripts written against the A1 surface. Additional LLM /
    embedder backends would extend this switch alongside their factory
    branch in :mod:`opshub.llm.factory` / :mod:`opshub.vectors.factory`.

    The ``connector:ms365`` and ``connector:box`` targets are
    special-cased: both credentials are OAuth refresh tokens rather
    than single-string bearers, so this command dispatches to the
    respective interactive paste-code flow in
    :mod:`opshub.cli._ms365_oauth` / :mod:`opshub.cli._box_oauth`. The
    ``--token`` flag is ignored for those targets (the OAuth flow has
    no use for a pre-baked token), which we surface as an explicit
    warning rather than silently dropping it.

    The Phase 9 ``connector:box_drive`` target (ADR-0019) is rejected
    with an actionable error: the local-filesystem-backed Box Drive
    connector reads the host FS directly and has *no* token / OAuth
    surface. Operators configure it via
    ``[connectors.box_drive] root_path`` in ``opshub.toml`` (or rely
    on the platform default), so accepting an ``auth set`` invocation
    here would only mislead.

    Security: there is intentionally no ``auth get`` command — we never
    echo tokens to stdout. The env-var override is the documented
    escape hatch for testing / debugging.
    """
    # Lazy imports keep CLI cold start fast (ADR-0001) and keep this
    # module compatible with ``tests/integration/test_cli_imports``.
    from opshub.core.secrets import set_secret

    if name == "connector:ms365":
        # MS365 needs an interactive OAuth dance; ``--token`` is
        # meaningless on this path and silently dropping it would
        # confuse operators who tried to script the auth set.
        if token is not None:
            typer.echo(
                "warning: --token is ignored for connector:ms365 "
                "(OAuth paste-code flow is used instead)",
                err=True,
            )
        from opshub.cli._ms365_oauth import run_paste_code_flow as run_ms365

        run_ms365()
        return

    if name == "connector:box":
        # Box mirrors the MS365 paste-code OAuth flow (Phase 7 step C1).
        # The ``--token`` flag has no meaning here — the refresh token
        # is produced by the OAuth exchange, not pasted in. Warn rather
        # than silently drop it so a misconfigured script surfaces the
        # mistake.
        if token is not None:
            typer.echo(
                "warning: --token is ignored for connector:box "
                "(OAuth paste-code flow is used instead)",
                err=True,
            )
        from opshub.cli._box_oauth import run_paste_code_flow as run_box

        run_box()
        return

    if name == "connector:box_drive":
        # Phase 9 (ADR-0019) ``box_drive`` connector has no token /
        # OAuth surface at all — it reads a local Box Drive mount
        # point on the host filesystem. Operators configure it via
        # ``[connectors.box_drive] root_path`` in ``opshub.toml`` (or
        # rely on the platform default: WSL2=/mnt/b, macOS=~/Box).
        # Accepting an ``auth set`` invocation here would silently
        # write a token nobody reads, so we fail-fast with an
        # actionable pointer to the ADR + setup doc.
        typer.echo(
            "box_drive connector does not use OAuth or paste-code auth. "
            "Configure root_path in opshub.toml under [connectors.box_drive] "
            "(or rely on the platform default: WSL2=/mnt/b, macOS=~/Box). "
            "See docs/adr/0019-local-filesystem-backed-connector.md for details.",
            err=True,
        )
        raise typer.Exit(code=2)

    if name == "github":
        from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY

        key = GITHUB_PAT_SECRET_KEY
    elif name == "connector:teams":
        # Phase 11 F5 (ADR-0010 §改訂 (d)): the Teams Microsoft Graph
        # User Token is stored under ``connector:teams:token`` so the
        # CLI writer + TeamsAuth reader cannot drift (mirrors the
        # Slack / MS365 / Box precedent). Unlike MS365 / Box this
        # connector accepts a pre-resolved token directly rather than
        # running an in-process OAuth dance — operators acquire the
        # token via Azure Portal / MSAL device code flow and paste
        # the result into ``--token`` (or stdin).
        from opshub.connectors.teams.auth import TEAMS_TOKEN_SECRET_KEY

        key = TEAMS_TOKEN_SECRET_KEY
    elif name in ("slack", "connector:slack"):
        # Phase 7 step A1 (principal updated in Phase 7.x per ADR-0018):
        # the Slack OAuth access token is stored under
        # ``connector:slack:token`` so the CLI writer + SlackAuth
        # reader cannot drift (mirrors the GitHub PAT precedent).
        # User Token (``xoxp-``) is the first-class principal; Bot
        # Token (``xoxb-``) is accepted as an alternative for
        # workspace-policy / audit-policy constraints.
        # Both ``slack`` (the original A1 form, kept for backward
        # compatibility) and ``connector:slack`` (the Phase 7 plan
        # §1 #4 namespace, matched by sibling MS365 / Box) route to
        # the same keyring key — see the Phase 7 follow-up PR for the
        # rationale (CLI namespace consistency across the 3 Phase 7
        # connectors). The recommended form is ``connector:slack``;
        # the bare ``slack`` alias is retained so any operator scripts
        # written against the A1 surface continue to work.
        # Lazy-imported per-branch so the ``slack_sdk`` import path
        # (deferred inside ``SlackAuth.test_token``) stays off the
        # cold-start path entirely when the operator never uses Slack.
        from opshub.connectors.slack.auth import SLACK_TOKEN_SECRET_KEY

        key = SLACK_TOKEN_SECRET_KEY
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
            "github, connector:slack (or legacy slack), embedder:openai, "
            "embedder:voyage, llm:anthropic, llm:openai, connector:ms365, "
            "connector:box, connector:teams "
            "(connector:box_drive uses opshub.toml, not auth set)",
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


@auth_app.command("test")
def auth_test(
    name: str = typer.Argument(
        ...,
        help=(
            "Connector name to verify (github, slack / connector:slack, "
            "connector:ms365, connector:box)."
        ),
    ),
) -> None:
    """Verify a stored connector token by hitting its SaaS auth-check endpoint.

    Calls each connector's ``test_token()`` helper which authenticates
    a live API call (Slack ``auth.test`` / GitHub ``GET /user`` /
    Microsoft Graph ``GET /me`` / Box ``users#me``) and prints the
    operator-relevant fields (username, principal, expiry, scopes).

    Exit codes:

    * ``0`` — verification succeeded; token is valid
    * ``1`` — verification failed (token revoked, network error,
      missing scope, etc.). The error message surfaces the failure
      reason but **never** the token itself — only API error codes or
      exception type names surface, matching the per-connector
      token-leak invariant.
    * ``2`` — unknown connector name (usage error)

    Security: this command performs a live API call. Network failures
    bubble up as ``ConfigError`` → exit 1. The token is loaded via the
    same precedence rule as the rest of opshub (env var override wins
    over keyring per ADR-0014).

    Supported targets mirror :func:`auth_set` for the SaaS connectors
    only — embedder / LLM auth verification is intentionally out of
    scope (those backends have their own verification surfaces in their
    respective factories).
    """
    # Lazy import per dispatch branch keeps the cold-start path light
    # (ADR-0001) — operators on the ``opshub --help`` path never pay
    # the SDK / httpx import cost.
    from opshub.core.errors import ConfigError

    # Resolve the connector-specific verification callable. Each arm
    # returns a zero-arg callable that, when invoked, performs the live
    # API check and returns a ``dict[str, str]``. Keeping the dispatch
    # focussed on resolution (and the call + error-handling unified
    # below) cuts the function size roughly in half versus the original
    # all-in-one if/elif tree.
    try:
        verifier = _resolve_auth_test_verifier(name)
    except _UnknownAuthTargetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ConfigError as exc:
        # Pre-flight ``ConfigError`` (e.g. MS365 / Box client_id
        # unconfigured) is surfaced as exit 1 with the same status:
        # failed framing as a runtime verification failure.
        typer.echo(f"connector: {name}", err=True)
        typer.echo("status:    failed", err=True)
        typer.echo(f"error:     {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        result: dict[str, str] = verifier()
    except ConfigError as exc:
        # ConfigError already carries a sanitised message (no token
        # substrings) per each connector's token-leak invariant. We
        # surface it verbatim and exit 1 so scripts / CI can branch on
        # the exit code.
        typer.echo(f"connector: {name}", err=True)
        typer.echo("status:    failed", err=True)
        typer.echo(f"error:     {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Success path: print the connector header + status + result fields
    # in a stable column-aligned format. The order is determined by the
    # connector's ``test_token()`` return-dict order (Python dicts are
    # insertion-ordered) so each connector controls its own display.
    typer.echo(f"connector: {name}")
    typer.echo("status:    ok")
    # Find the widest key for column alignment. Cap at 20 chars so a
    # future overly-long key name doesn't blow out the layout.
    key_width = min(max((len(k) for k in result), default=0), 20)
    for k, v in result.items():
        # Empty values (e.g. user without a configured display name)
        # render as ``(none)`` rather than a blank line — operator
        # readability beats strict round-trip fidelity here.
        display = v if v else "(none)"
        typer.echo(f"{k:<{key_width}}  {display}")


class _UnknownAuthTargetError(Exception):
    """Raised by :func:`_resolve_auth_test_verifier` for unknown connector names.

    :func:`auth_test` catches this to map to exit code 2 (usage error)
    while keeping :class:`ConfigError` (operational failure) on the
    exit-code-1 path. Using a dedicated exception type rather than
    returning ``None`` makes the dispatch's intent self-documenting at
    the type level.
    """


def _resolve_auth_test_verifier(
    name: str,
) -> Callable[[], dict[str, str]]:
    """Return a zero-arg callable that verifies the given connector's token.

    Centralises the per-connector wiring (SDK import, config lookup,
    constructor) so :func:`auth_test` itself stays focussed on the
    universal display + error-handling path. Each arm:

    1. Lazy-imports the connector's auth module (cold-start budget).
    2. Resolves any required config (e.g. ``client_id``) and raises
       :class:`~opshub.core.errors.ConfigError` if missing — the CLI
       maps that to exit 1 with the same ``status: failed`` framing as
       a runtime API failure, so operators see a uniform UX.
    3. Returns ``connector.test_token`` (bound method or module
       function) so the caller can invoke it inside a single
       ``try/except ConfigError`` block.

    Unknown connector names raise :class:`_UnknownAuthTargetError` with the
    supported-list error message ready to print verbatim — mirrors the
    ``unknown auth target`` error UX used by :func:`auth_set`.
    """
    from opshub.core.errors import ConfigError

    if name == "github":
        from opshub.connectors.github.auth import test_token as github_test_token

        return github_test_token

    if name in ("slack", "connector:slack"):
        from opshub.connectors.slack.auth import SlackAuth

        return SlackAuth().test_token

    if name == "connector:ms365":
        # MS365 needs the configured client_id to build the MSAL
        # PublicClientApplication. The error message points the
        # operator at both ``opshub init`` (which writes the starter
        # config) and the exact section / field name so they can edit
        # the existing file if ``opshub init`` already ran.
        from opshub.connectors.ms365.auth import MS365Auth
        from opshub.core.config import OpsHubSettings

        settings = OpsHubSettings()
        ms365_client_id = settings.connectors.ms365.client_id
        if not ms365_client_id:
            raise ConfigError(
                "MS365 client_id is not configured. Set "
                "`[connectors.ms365] client_id` in "
                f"{settings.config_dir}/config.toml "
                "(run `opshub init` first if the file does not exist yet)."
            )
        return MS365Auth(client_id=ms365_client_id).test_token

    if name == "connector:box":
        from opshub.connectors.box.auth import BoxAuth
        from opshub.core.config import OpsHubSettings

        settings = OpsHubSettings()
        box_client_id = settings.connectors.box.client_id
        if not box_client_id:
            raise ConfigError(
                "Box client_id is not configured. Set "
                "`[connectors.box] client_id` in "
                f"{settings.config_dir}/config.toml "
                "(run `opshub init` first if the file does not exist yet)."
            )
        return BoxAuth(client_id=box_client_id).test_token

    raise _UnknownAuthTargetError(
        f"unknown auth target {name!r}; currently supported: "
        "github, connector:slack (or legacy slack), "
        "connector:ms365, connector:box"
    )


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
