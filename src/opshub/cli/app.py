"""Typer CLI entry point.

Phase 1 step 3 skeleton: provides `opshub version` as a smoke command.
Step 13 added `opshub init` and `opshub db migrate` for first-time setup and
on-demand schema upgrades. Subcommand callbacks defer heavy imports
(``opshub.core``, ``opshub.db``, ``alembic``) to call time so that
``opshub --help`` cold start stays under ~300ms (ADR-0001).

Phase 2 step 1 adds a top-level :class:`OpsHubError` handler in
:func:`main` (the console-script entry point). Domain failures surface
as a single ``Error: <message>`` line on stderr plus a meaningful exit
code, rather than leaking a Python traceback at the user. The wrapper
only sits on :func:`main` so existing tests using
``typer.testing.CliRunner`` (which invokes ``app`` directly) still see
the raw exception on ``result.exception``.

ADR-0027 (T2 of #317) adds five global troubleshooting options to the
root callback — ``-v`` / ``-q`` / ``--debug`` / ``--log-format`` /
``--log-file`` — wired through :func:`opshub.core.logging.resolve_log_settings`
and :func:`opshub.core.logging.configure_logging`. The wiring is
deliberately deferred (lazy import inside the callback body) so that
``opshub --help`` and ``opshub --version`` do not pay for the
structlog import and configuration. The resolved
:class:`~opshub.core.logging.LogSettings` is stored on the Typer
``Context`` (``ctx.obj``) so subcommands (notably ``<connector> sync``
and ``mcp serve`` in T3) can read the ``debug`` flag without
re-resolving. ``--debug`` and ``-vv`` additionally set
``OPSHUB_DEBUG=1`` in the environment so that subprocess paths (MCP
serve) which never see the parent ``ctx`` still receive the signal.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

from opshub import __version__
from opshub.cli.agent import agent_app
from opshub.cli.box import box_app
from opshub.cli.box_drive import box_drive_app
from opshub.cli.brief import register as register_brief
from opshub.cli.commitment import commitment_app
from opshub.cli.connectors import connectors_app
from opshub.cli.decision import decision_app
from opshub.cli.embedder import embedder_app
from opshub.cli.embeddings import embeddings_app
from opshub.cli.github import github_app
from opshub.cli.google_calendar import google_calendar_app
from opshub.cli.google_mail import google_mail_app
from opshub.cli.google_workspace import google_workspace_app
from opshub.cli.graph import graph_app
from opshub.cli.handoff import handoff_app
from opshub.cli.inbox import inbox_app
from opshub.cli.link import link_app
from opshub.cli.llm import llm_app
from opshub.cli.lock import lock_app
from opshub.cli.mcp import mcp_app
from opshub.cli.ms365 import ms365_app
from opshub.cli.onedrive_drive import onedrive_drive_app
from opshub.cli.person import person_app
from opshub.cli.projections import projections_app
from opshub.cli.propose import propose_app
from opshub.cli.recall import register as register_recall
from opshub.cli.search import register as register_search
from opshub.cli.session import session_app
from opshub.cli.skills import skills_app
from opshub.cli.slack import slack_app
from opshub.cli.task import task_app
from opshub.cli.teams import teams_app
from opshub.cli.web import web_app
from opshub.cli.workspace import workspace_app

app = typer.Typer(
    name="opshub",
    help="Local-first operational memory and execution hub for humans and AI agents.",
    no_args_is_help=True,
)

db_app = typer.Typer(
    name="db",
    help="Database operations.",
    no_args_is_help=True,
)
app.add_typer(db_app)
app.add_typer(projections_app)
app.add_typer(embeddings_app)
app.add_typer(task_app)
app.add_typer(inbox_app)
app.add_typer(decision_app)
app.add_typer(lock_app)
app.add_typer(handoff_app)
app.add_typer(session_app)
app.add_typer(agent_app)
app.add_typer(workspace_app)
# Phase 17-B (ADR-0031): per-noun connector groups replace the legacy
# ``opshub connector <verb>`` 3-level surface. Order matches the
# Phase 7 / 9 / 11 / 13 / 14 introduction order so the ``--help``
# listing follows the chronological codebase narrative.
app.add_typer(slack_app)
app.add_typer(github_app)
app.add_typer(ms365_app)
app.add_typer(box_app)
app.add_typer(teams_app)
app.add_typer(google_workspace_app)
app.add_typer(google_mail_app)
app.add_typer(google_calendar_app)
app.add_typer(box_drive_app)
app.add_typer(onedrive_drive_app)
# Phase 21-C (ADR-0037): browser-rendered Web page connector.
app.add_typer(web_app)
app.add_typer(connectors_app)
app.add_typer(embedder_app)
app.add_typer(llm_app)
app.add_typer(propose_app)
app.add_typer(link_app)
app.add_typer(graph_app)
app.add_typer(person_app)
# Phase 25-C (ADR-0042): two-way commitment ledger (旗艦 of 秘書化 v1).
app.add_typer(commitment_app)
app.add_typer(mcp_app)
app.add_typer(skills_app)
register_recall(app)
register_search(app)
register_brief(app)


def _version_callback(value: bool) -> None:
    """Eager ``--version`` handler: echo the package version and exit."""
    if value:
        typer.echo(f"opshub {__version__}")
        raise typer.Exit()


@app.callback()
def _root(  # pyright: ignore[reportUnusedFunction]
    ctx: typer.Context,
    version: Annotated[  # pyright: ignore[reportUnusedParameter]
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed opshub version and exit.",
        ),
    ] = None,
    progress: Annotated[
        bool | None,
        typer.Option(
            "--progress/--no-progress",
            help=(
                "Show a progress indicator for long-running commands "
                "(connector sync, embeddings rebuild, projections rebuild). "
                "Default: auto-detect (on when stderr is a TTY)."
            ),
        ),
    ] = None,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help=(
                "Increase log verbosity (repeatable). "
                "``-v`` lifts the level to INFO, ``-vv`` to DEBUG. "
                "Mutually exclusive with ``--quiet``; ``--debug`` overrides both."
            ),
        ),
    ] = 0,
    quiet: Annotated[
        int,
        typer.Option(
            "--quiet",
            "-q",
            count=True,
            help=(
                "Decrease log verbosity (repeatable). "
                "``-q`` lowers the level to WARNING, ``-qq`` to ERROR."
            ),
        ),
    ] = 0,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help=(
                "Enable DEBUG-level logging and print a sanitised "
                "traceback on uncaught OpsHubError. Implies ``-vv``."
            ),
        ),
    ] = False,
    log_format: Annotated[
        str,
        typer.Option(
            "--log-format",
            help=(
                "Log renderer: ``auto`` (default, JSON when stderr is "
                "not a TTY, console otherwise), ``json``, or ``console``."
            ),
        ),
    ] = "auto",
    log_file: Annotated[
        str | None,
        typer.Option(
            "--log-file",
            help=(
                "Tee the log stream to this file (created with mode 0600). "
                "Redaction applies to the file content."
            ),
        ),
    ] = None,
) -> None:
    """Root callback. Required so that single-subcommand mode is not used; this keeps
    `opshub <subcommand>` invocation stable as more commands are added in Phase 1.

    The ``--version`` flag is wired here (rather than as a separate command)
    so that ``opshub --version`` is recognised before any subcommand
    parsing. The existing ``opshub version`` subcommand is preserved
    below and produces identical output.

    The ``--progress`` / ``--no-progress`` flag records the operator's
    progress-display preference before any subcommand runs (issue #316).

    The ``-v`` / ``-q`` / ``--debug`` / ``--log-format`` / ``--log-file``
    flags (ADR-0027, T2 of #317) configure structlog before any
    subcommand runs. The wiring is intentionally deferred:

    * ``configure_logging`` is **only** called when the callback body
      actually runs — Typer short-circuits the body for ``--help`` and
      eager ``--version`` callbacks, so the structlog import / setup
      cost is never paid for help / version queries (R6, cold-start
      budget).
    * The import of :mod:`opshub.core.logging` is lazy (inside the
      function body) so :file:`tests/integration/test_cli_imports.py`
      stays green — that test bans top-level ``opshub.core`` imports
      inside :mod:`opshub.cli.app` precisely to protect the cold-start
      budget.
    * The resolved :class:`LogSettings` is stored on ``ctx.obj`` so
      subcommands can read the ``debug`` flag (T3 reads it from
      ``ctx.obj["debug"]`` for connector sync / MCP error rendering).
    * When DEBUG-level is in effect (``--debug`` or ``-vv``), the
      callback also exports ``OPSHUB_DEBUG=1`` so subprocess paths
      (e.g. ``mcp serve`` re-execed without inheriting the parent
      ``ctx``) pick up the signal.
    """
    # Lazy imports keep ``opshub --help`` under the ADR-0001 cold-start
    # budget and satisfy ``test_cli_imports`` (which bans top-level
    # ``opshub.core`` imports in this module).
    from opshub.cli import _progress
    from opshub.core.logging import configure_logging, resolve_log_settings

    _progress.set_preference(progress)

    settings = resolve_log_settings(
        verbose=verbose,
        quiet=quiet,
        debug=debug,
        log_format=log_format,
        log_file=log_file,
    )

    # Translate ``auto`` into ``json=None`` (let ``configure_logging``
    # probe the stderr TTY); ``json`` / ``console`` are explicit overrides.
    if settings.log_format == "json":
        json_flag: bool | None = True
    elif settings.log_format == "console":
        json_flag = False
    else:  # "auto"
        json_flag = None

    configure_logging(
        level=settings.level,
        json=json_flag,
        log_file=settings.log_file,
    )

    # Expose the resolved settings to subcommands. T3 reads
    # ``ctx.obj["debug"]`` from connector sync / MCP rendering paths.
    # ``ensure_object`` would also work but we want exactly this shape.
    ctx.obj = {"log_settings": settings, "debug": settings.debug}

    # Mirror the debug signal into the environment so subprocess paths
    # (``mcp serve`` re-execed without inheriting ``ctx``; cron-driven
    # sync; tests that read ``OPSHUB_DEBUG`` after the callback ran)
    # observe the same flag. We only **set** ``OPSHUB_DEBUG``; we never
    # *unset* a pre-existing value, so a user who exported it remains
    # in debug mode for the duration of the process.
    if settings.debug:
        os.environ["OPSHUB_DEBUG"] = "1"


@app.command()
def version() -> None:
    """Show the installed opshub version."""
    typer.echo(f"opshub {__version__}")


@app.command("init")
def init(
    *,
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing config.toml with the starter template.",
    ),
    install_skills: bool | None = typer.Option(
        None,
        "--install-skills/--no-install-skills",
        help=(
            "Install the 14 bundled assistant skills to ~/.claude/skills/ + "
            "~/.agents/skills/ (Phase 16-C, ADR-0029). "
            "Default: prompt on TTY (default yes), install on non-TTY. "
            "Use --no-install-skills to skip; use `opshub skills install` "
            "later to update or switch scopes."
        ),
    ),
) -> None:
    """First-time setup: create dirs, write starter config, apply migrations, install skills."""
    # Lazy import: heavy modules (pydantic_settings, alembic) load only when
    # the command actually runs.
    from opshub.cli.init import init_command

    init_command(force=force, install_skills=install_skills)


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending Alembic migrations."""
    # Lazy import: see module docstring.
    from opshub.cli.db import migrate_command

    migrate_command()


def _debug_active() -> bool:
    """Return True iff ``--debug`` or ``OPSHUB_DEBUG`` was effective.

    The root callback exports ``OPSHUB_DEBUG=1`` when ``--debug`` (or
    ``-vv``) is set. ``main()`` consults this env var (rather than
    threading the ``LogSettings`` back from the callback) because
    Typer / Click have already torn down the context by the time the
    exception reaches the wrapper. The env-var probe also picks up
    operators who exported ``OPSHUB_DEBUG=1`` directly (e.g. in cron).
    """
    raw = os.environ.get("OPSHUB_DEBUG")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on", "debug"}


def main() -> None:
    """Console script entry point (referenced by ``[project.scripts].opshub``).

    Wraps the Typer ``app`` so that :class:`OpsHubError` subclasses surface
    as a single ``Error: <message>`` line on stderr plus a meaningful exit
    code, rather than leaking a Python traceback at the user:

    * :class:`ValidationError` (bad command input) → exit code 2 — mirrors
      the conventional Unix "usage error" code that Typer itself returns
      for argument parsing failures.
    * Any other :class:`OpsHubError` (config / not-found / conflict) →
      exit code 1 — the generic "command failed" code.

    The catch order matters: :class:`ValidationError` is a subclass of
    :class:`OpsHubError`, so the narrower handler must come first.

    ADR-0027 (T2 of #317) adds ``--debug`` semantics:

    * Default (no ``--debug``): the existing one-line ``Error: <msg>``
      output. The message is passed through
      :func:`opshub.core.sanitise.sanitise_error_message` so that an
      ``OpsHubError`` whose message accidentally embeds a token (a
      connector exception bubbling up through a service layer that
      forgot to scrub) is still marker-redacted before reaching
      stderr (R2).
    * ``--debug`` (or ``OPSHUB_DEBUG=1``): a sanitised full traceback
      via :func:`opshub.core.logging.format_debug_traceback` is printed
      to stderr **in addition** to the one-line ``Error:`` summary.
      The traceback is also sanitised, so operators can safely paste
      it into an issue or chat.
    * Non-:class:`OpsHubError` exceptions (real bugs) propagate as
      before. In ``--debug`` mode we additionally render a sanitised
      traceback before re-raising so operators see the same output
      shape; the actual exception is left to bubble up so CI keeps
      the original ``__traceback__`` for log aggregation.

    Implementation notes:

    * ``SystemExit`` is raised (not ``typer.Exit``) because the Typer /
      Click runtime is no longer on the stack by the time ``app()`` has
      returned or raised — ``typer.Exit`` is a ``RuntimeError`` subclass
      that only Click's runtime knows how to translate into an exit
      code. Raising ``SystemExit`` directly is the idiomatic way to
      surface a process exit code from a plain ``main()`` entry point.
    * The exception types and the sanitiser / traceback helper are
      imported lazily so that
      :file:`tests/integration/test_cli_imports.py` can keep
      ``opshub.cli.app``'s module-level surface limited to ``typer``
      and the sub-app objects (ADR-0001 cold-start discipline).
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001) and satisfy the
    # ``test_cli_imports`` static check that bans top-level
    # ``opshub.core`` imports inside ``opshub.cli.*``.
    from opshub.core.errors import OpsHubError, ValidationError
    from opshub.core.logging import format_debug_traceback
    from opshub.core.sanitise import sanitise_error_message

    try:
        app()
    except ValidationError as exc:
        message = sanitise_error_message(str(exc))
        if _debug_active():
            typer.echo(format_debug_traceback(exc), err=True)
        typer.echo(f"Error: {message}", err=True)
        raise SystemExit(2) from exc
    except OpsHubError as exc:
        message = sanitise_error_message(str(exc))
        if _debug_active():
            typer.echo(format_debug_traceback(exc), err=True)
        typer.echo(f"Error: {message}", err=True)
        raise SystemExit(1) from exc
    except Exception as exc:
        # Real bugs (non-OpsHubError) bubble up so CI captures the
        # native traceback. In ``--debug`` mode we additionally emit
        # the sanitised traceback so operators get the same shape on
        # stderr as for OpsHubError.
        if _debug_active():
            typer.echo(format_debug_traceback(exc), err=True)
        raise


if __name__ == "__main__":
    main()
