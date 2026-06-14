"""``opshub commitment`` — two-way commitment ledger (Phase 25-C, ADR-0042).

The旗艦 of the 秘書化 v1 epic. Five subcommands surface the operator-facing
view of the commitment ledger the
:class:`~opshub.services.commitments.CommitmentScanService` mines from the
``sources`` opshub already ingests:

* ``opshub commitment scan [--since <id>]`` — the manual, on-demand
  extraction pass. Reads sources observed since the last scan (or since
  ``--since`` when supplied), calls the LLM per source, and records the
  extracted commitments. **Requires an LLM backend** (long-running CLI →
  progress, ADR-0026); ``[llm] backend = "disabled"`` short-circuits to a
  clean hint.
* ``opshub commitment list [--direction i-owe|owed-to-me] [--open]
  [--person <id>]`` — read the ledger. **No LLM call** — works offline /
  with the backend disabled.
* ``opshub commitment resolve|dismiss|reopen <id>`` — operator-driven
  state transitions (the ledger is a read signal; no external nudge is
  sent, ADR-0042 §督促境界).

Cold-start guard
----------------
Module-level imports stay within the cold-start whitelist (``__future__``
/ ``typer`` / ``typing``). Every ``opshub`` import lives inside a command
body so the cold-start budget is paid only when a commitment subcommand
runs (ADR-0001).

Exit-code contract
------------------
* ``0`` — success.
* ``1`` — :class:`~opshub.core.errors.OpsHubError` raised by the service
  (e.g. resolving a missing / already-resolved commitment).
* ``2`` — :class:`~opshub.core.errors.ConfigError`: most commonly the
  ``[llm] backend = "disabled"`` short-circuit on ``scan``; also a DB not
  initialised, or a malformed ``--direction`` value.
"""

from __future__ import annotations

from typing import Annotated

import typer

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

commitment_app = typer.Typer(
    name="commitment",
    help="Two-way commitment ledger (scan / list / resolve / dismiss / reopen).",
    no_args_is_help=True,
)


# CLI ``--direction`` spelling → event/projection value. The CLI uses
# kebab-case (``i-owe`` / ``owed-to-me``) to match the flag conventions;
# the stored value is snake_case (``i_owe`` / ``owed_to_me``).
_DIRECTION_ALIASES = {"i-owe": "i_owe", "owed-to-me": "owed_to_me"}


@commitment_app.command("scan")
def commitment_scan(
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "Source-id watermark to scan from (advanced; default resumes "
                "from the stored commitment-scan cursor)."
            ),
        ),
    ] = None,
    max_sources: Annotated[
        int,
        typer.Option(
            "--max-sources",
            help="Cap on the number of sources read in this scan (default 200).",
        ),
    ] = 200,
) -> None:
    """Extract commitments from sources observed since the last scan.

    Requires an LLM backend (the extraction is a non-deterministic body
    read, ADR-0042). On success, prints a one-line summary of how many
    sources were scanned and how many commitments were extracted.
    """
    from opshub.cli import _progress
    from opshub.cli._wiring import build_commitment_scan_service
    from opshub.core.config import OpsHubSettings
    from opshub.core.errors import ConfigError, OpsHubError

    settings = OpsHubSettings()
    if settings.llm.backend == "disabled":
        # Short-circuit BEFORE wiring so the operator running ``scan``
        # straight after install gets a clean hint rather than a stack
        # trace (symmetric with ``opshub propose generate``). ``list`` /
        # ``resolve`` still work with the backend disabled.
        typer.echo(
            "[llm] backend is disabled; configure 'anthropic' or 'openai' "
            "in opshub.toml (or set OPSHUB_LLM_BACKEND env var) and run "
            "`opshub llm auth set <backend>` to store the API key. "
            "(commitment list / resolve / dismiss / reopen work without an LLM.)",
            err=True,
        )
        raise typer.Exit(code=2)

    service = build_commitment_scan_service(actor="cli:commitment_scan")
    if since is not None:
        # ``--since`` is an advanced override: the operator pins a source-id
        # floor to re-scan from. We honour it by seeding the cursor read in
        # the service via the public scan API, which already resumes from
        # the stored cursor — so we instead expose it as a one-shot floor
        # by temporarily widening the watermark. The service reads the
        # stored cursor; to override we pass through a dedicated path.
        try:
            summary = service.scan_from(since, max_sources=max_sources)
        except ConfigError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        except OpsHubError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    else:
        try:
            with _progress.indeterminate("scanning sources for commitments"):
                summary = service.scan(max_sources=max_sources)
        except ConfigError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        except OpsHubError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    typer.echo(
        f"Scanned {summary.sources_scanned} source(s); "
        f"extracted {summary.commitments_extracted} commitment(s)."
    )


@commitment_app.command("list")
def commitment_list(
    direction: Annotated[
        str | None,
        typer.Option(
            "--direction",
            help="Filter by direction: i-owe | owed-to-me.",
        ),
    ] = None,
    open_only: Annotated[
        bool,
        typer.Option(
            "--open",
            help="Show only open commitments (hide resolved / dismissed).",
        ),
    ] = False,
    person: Annotated[
        str | None,
        typer.Option(
            "--person",
            help="Filter by counterparty person id (ULID or 'person:<id>').",
        ),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: table | json.",
        ),
    ] = "table",
) -> None:
    """List commitments from the ``commitments`` ledger (no LLM call)."""
    import json as _json

    from opshub.cli._wiring import build_commitment_scan_service
    from opshub.core.errors import ConfigError

    if fmt not in {"table", "json"}:
        typer.echo(
            f"Error: unsupported format {fmt!r}; choose 'table' or 'json'.",
            err=True,
        )
        raise typer.Exit(code=2)

    resolved_direction: str | None = None
    if direction is not None:
        resolved_direction = _DIRECTION_ALIASES.get(direction)
        if resolved_direction is None:
            typer.echo(
                f"Error: invalid --direction {direction!r}; expected 'i-owe' or 'owed-to-me'.",
                err=True,
            )
            raise typer.Exit(code=2)

    # Accept both ``<ulid>`` and ``person:<ulid>`` so the operator can paste
    # a ref straight from ``commitment list`` output.
    person_ref: str | None = None
    if person is not None:
        person_ref = person if person.startswith("person:") else f"person:{person}"

    service = build_commitment_scan_service()
    try:
        commitments = service.list_commitments(
            direction=resolved_direction,
            state="open" if open_only else None,
            person=person_ref,
        )
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if fmt == "json":
        payload = [
            {
                "id": c.id,
                "source_id": c.source_id,
                "source_type": c.source_type,
                "direction": c.direction,
                "counterparty": c.counterparty,
                "due": c.due,
                "text": c.text,
                "confidence": c.confidence,
                "state": c.state,
            }
            for c in commitments
        ]
        typer.echo(_json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not commitments:
        typer.echo("(no commitments — run `opshub commitment scan` first)")
        return

    for c in commitments:
        arrow = "→ I owe" if c.direction == "i_owe" else "← owed to me"
        due = f" (due {c.due})" if c.due else ""
        party = f" [{c.counterparty}]" if c.counterparty else ""
        typer.echo(f"{c.id}  [{c.state}] {arrow}{party}{due}")
        typer.echo(f"    {c.text}")


@commitment_app.command("resolve")
def commitment_resolve(
    commitment_id: Annotated[
        str,
        typer.Argument(metavar="COMMITMENT_ID", help="ULID of the commitment."),
    ],
) -> None:
    """Mark a commitment done (operator HITL, ADR-0042)."""
    from opshub.cli._wiring import build_commitment_scan_service
    from opshub.core.errors import ConfigError, OpsHubError

    service = build_commitment_scan_service(actor="cli:commitment_resolve")
    try:
        service.resolve(commitment_id)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Resolved commitment {commitment_id}")


@commitment_app.command("dismiss")
def commitment_dismiss(
    commitment_id: Annotated[
        str,
        typer.Argument(metavar="COMMITMENT_ID", help="ULID of the commitment."),
    ],
    reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            help="Optional free-form note for the audit log (<=1000 chars).",
        ),
    ] = None,
) -> None:
    """Mark an extraction a false positive (operator HITL, ADR-0042)."""
    from opshub.cli._wiring import build_commitment_scan_service
    from opshub.core.errors import ConfigError, OpsHubError

    service = build_commitment_scan_service(actor="cli:commitment_dismiss")
    try:
        service.dismiss(commitment_id, reason=reason)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Dismissed commitment {commitment_id}")


@commitment_app.command("reopen")
def commitment_reopen(
    commitment_id: Annotated[
        str,
        typer.Argument(metavar="COMMITMENT_ID", help="ULID of the commitment."),
    ],
) -> None:
    """Re-open a resolved / dismissed commitment (operator HITL, ADR-0042)."""
    from opshub.cli._wiring import build_commitment_scan_service
    from opshub.core.errors import ConfigError, OpsHubError

    service = build_commitment_scan_service(actor="cli:commitment_reopen")
    try:
        service.reopen(commitment_id)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except OpsHubError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Reopened commitment {commitment_id}")
