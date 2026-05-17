"""``opshub brief "<topic>"`` — generate an LLM-backed operational briefing.

Phase 5 step B4 (ADR-0015) ships the operator-facing surface for the
briefing flow that PR #91 (BriefingService, step B3) wired underneath.
Calls :meth:`BriefingService.generate` with the supplied topic + optional
scope / limits, renders the resulting :class:`Briefing` markdown to
stdout (or JSON via ``--format json``), and optionally saves to
``workspace/briefings/<slug>-<briefing-id>.md`` via ``--save``.

Shape rationale
---------------

Like :mod:`opshub.cli.recall`, ``brief`` has no sub-verbs — the topic
goes straight to the top-level command and the flags decorate it. We
register the command as a flat :meth:`Typer.command` on the root app
(not as a nested :class:`Typer`) so ``opshub brief "topic" --format
json`` parses cleanly under Click 8 (a Typer sub-app would expect a
``COMMAND`` token after the group options; see :mod:`opshub.cli.recall`
module docstring for the full reasoning).

Module-level imports stay limited to ``__future__`` / ``typing`` /
``typer`` so ``opshub --help`` cold start stays under the ADR-0001
budget. The ``test_cli_imports`` static check enforces this on every
CI run; the heavy paths (settings, wiring, error types,
:class:`Briefing`, :func:`Path` for the save target) load lazily
inside the command body.

Exit-code contract
------------------

* ``[llm] backend = "disabled"`` → exit 2 + stderr setup hint. We
  short-circuit BEFORE :func:`build_briefing_service` so the operator
  running ``opshub brief`` straight after install (no LLM credentials
  configured) gets a clean message rather than a stack trace from the
  briefing service trying to open the engine.
* :class:`~opshub.core.errors.ConfigError` raised downstream (e.g.
  ``NoOpLLMClient.complete`` when the disabled-backend check above
  was bypassed by an env var) → exit 2 with the service-supplied
  remediation message.
* :class:`~opshub.core.errors.OpsHubError` (LLM provider failure
  surfaced via :class:`BriefingFailed`) → exit 1 with the sanitised
  message; the briefing audit row was already appended to the event
  log by :class:`BriefingService` before the re-raise so a follow-up
  ``opshub brief --history`` (Phase 5.x) can introspect the attempt.
* Success → exit 0 + the rendered briefing on stdout. When ``--save``
  is set, the saved path is also echoed on stderr so a piped
  ``opshub brief ... > out.md`` workflow still surfaces the
  side-effect.
"""

from __future__ import annotations

from typing import Annotated

import typer

# Heavy imports happen inside the command body (ADR-0001 lazy-import rule).


def brief_command(
    topic: Annotated[
        str,
        typer.Argument(help="The topic to brief on (free-form natural language)."),
    ],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Scope of the briefing (only 'all' is supported in Phase 5 MVP).",
        ),
    ] = "all",
    max_sources: Annotated[
        int,
        typer.Option(
            "--max-sources",
            help="Maximum number of related entities to feed the LLM.",
        ),
    ] = 20,
    max_tokens: Annotated[
        int,
        typer.Option(
            "--max-tokens",
            help="LLM response token cap (caller pays the cost — see ADR-0015 §決定 (h)).",
        ),
    ] = 1500,
    save: Annotated[
        bool,
        typer.Option(
            "--save",
            help=(
                "Persist the briefing as a Markdown file under "
                "``<workspace.root>/briefings/<slug>-<briefing-id>.md``."
            ),
        ),
    ] = False,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: md | json. Defaults to md.",
        ),
    ] = "md",
) -> None:
    """Generate a briefing for ``topic`` and render it to stdout.

    Wires the Phase 5 briefing flow (PR #91 BriefingService) and routes
    the disabled-backend / LLM-failure cases to the documented exit
    codes (2 / 1 respectively). See the module docstring for the full
    contract.
    """
    # Lazy imports keep CLI cold start fast (ADR-0001) and satisfy the
    # ``test_cli_imports`` module-level whitelist.
    from opshub.cli._render import render_briefing_json, render_briefing_md
    from opshub.cli._wiring import build_briefing_service
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError, OpsHubError
    from opshub.core.slug import slugify

    if fmt not in {"md", "json"}:
        # Mirror the ``_render.dispatch`` validation but keep the brief
        # CLI's format-set narrower (no ``table`` — a briefing is a
        # single record, not a list).
        typer.echo(
            f"Error: invalid --format {fmt!r}; expected one of 'md', 'json'",
            err=True,
        )
        raise typer.Exit(code=2)

    settings = OpsHubSettings()
    if settings.llm.backend == "disabled":
        # Short-circuit BEFORE :func:`build_briefing_service` so we never
        # open the SQLite engine / resolve the embedder when the operator
        # has not turned an LLM backend on. The hint mirrors the message
        # :class:`NoOpLLMClient` would have raised, surfaced earlier in
        # the lifecycle so the operator sees one clean line of stderr
        # instead of a service-side trace.
        typer.echo(
            "[llm] backend is disabled; configure 'anthropic' or 'openai' "
            "in opshub.toml (or set OPSHUB_LLM_BACKEND env var) and run "
            "`opshub connector auth set llm:<backend>` to store the API key.",
            err=True,
        )
        raise typer.Exit(code=2)

    service = build_briefing_service()
    try:
        briefing = service.generate(
            topic,
            scope=scope,
            max_sources=max_sources,
            max_tokens=max_tokens,
        )
    except ConfigError as exc:
        # Defensive: when an operator forces the env var but the wiring
        # ends up with a NoOpLLMClient anyway (or some other config
        # mismatch surfaces from the briefing service), translate to
        # exit code 2 — same shape as the pre-check above.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        # Anything else under the OpsHub error hierarchy is a hard
        # failure (e.g. LLM provider raised, surfaced by
        # :meth:`BriefingService.generate` after recording
        # :class:`BriefingFailed`). The audit event is already durable
        # at this point; we just translate the user-visible message.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if fmt == "json":
        typer.echo(render_briefing_json(briefing))
    else:
        typer.echo(render_briefing_md(briefing))

    if save:
        # The save path resolves at write time so a tmp-redirected
        # workspace root in tests sees the file land under the tmp dir.
        # ``slugify`` already falls back to ``"briefing"`` on empty
        # input; appending the ULID guarantees uniqueness even if two
        # briefings on the same topic land in the same directory.
        target = (
            settings.workspace.root / "briefings" / f"{slugify(topic)}-{briefing.briefing_id}.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(briefing.markdown, encoding="utf-8")
        typer.echo(f"saved briefing to {target}", err=True)


def register(app: typer.Typer) -> None:
    """Register :func:`brief_command` on the root Typer app.

    Mirrors :func:`opshub.cli.recall.register`: encapsulates the
    registration knob so :mod:`opshub.cli.app` only has to call
    ``register(app)``. The command name (``"brief"``) and short help
    stay co-located with the body.
    """
    app.command(
        name="brief",
        help="Generate an LLM-backed operational briefing on a topic.",
    )(brief_command)
