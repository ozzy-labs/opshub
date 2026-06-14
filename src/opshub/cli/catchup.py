"""``opshub catchup`` — surface "what changed since I last looked?" (Phase 25-E).

The 秘書化 v1 catchup surface (epic #566). Reads the existing read models
and bundles the diff that accrued since the operator last caught up:

* **new sources** observed since the seen-marker watermark,
* **open commitments** (overdue surfaced first) from the旗艦 ledger (25-C),
* **new Slack demand** (un-answered @mention / DM) since the watermark.

By default the run advances the seen marker so the next ``catchup`` resumes
from this point (``--since-last-seen`` is the documented spelling of this
default; ``--no-advance`` is a dry preview that leaves the marker put).

**No LLM call** — catchup is a pure read of the projections (works offline
/ with the backend disabled). The host (Claude Code etc.) applies a
``brief``-style summary on top of the structured digest via the ``catchup``
assistant skill (ADR-0015 §brief).

Shape rationale
---------------
Like :mod:`opshub.cli.brief` / :mod:`opshub.cli.recall`, ``catchup`` has no
sub-verbs — the flags decorate a flat top-level command. We register it via
:meth:`Typer.command` on the root app (not a nested :class:`Typer`) so the
flags parse cleanly under Click 8 (see :mod:`opshub.cli.recall` for the
reasoning).

Cold-start guard
----------------
Module-level imports stay within the cold-start whitelist (``__future__`` /
``typer`` / ``typing``). Every ``opshub`` import lives inside the command
body (ADR-0001).

Exit-code contract
------------------
* ``0`` — success (an empty diff is still success — "nothing new" is a
  valid answer).
* ``2`` — :class:`~opshub.core.errors.ConfigError` (e.g. the DB is not
  initialised).
"""

from __future__ import annotations

from typing import Annotated

import typer

# Heavy imports happen inside the command body (ADR-0001 lazy-import rule).


def catchup_command(
    since_last_seen: Annotated[
        bool,
        typer.Option(
            "--since-last-seen/--no-advance",
            help=(
                "Advance the seen marker after surfacing the diff so the next "
                "catchup resumes from here (default). Pass --no-advance for a "
                "dry preview that leaves the marker untouched."
            ),
        ),
    ] = True,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Per-section cap on listed items (counts always reflect the full diff).",
        ),
    ] = 20,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: text | json.",
        ),
    ] = "text",
) -> None:
    """Surface the diff since the last catchup; advance the marker by default."""
    import json as _json

    from opshub.cli._wiring import build_catchup_service
    from opshub.core.errors import ConfigError

    if fmt not in {"text", "json"}:
        typer.echo(
            f"Error: unsupported format {fmt!r}; choose 'text' or 'json'.",
            err=True,
        )
        raise typer.Exit(code=2)

    service = build_catchup_service(actor="cli:catchup")
    try:
        digest = service.catchup(advance=since_last_seen, limit=limit)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if fmt == "json":
        typer.echo(_json.dumps(_digest_json(digest), ensure_ascii=False, indent=2))
        return

    _render_text(digest)


def _digest_json(digest: object) -> dict[str, object]:
    """Render a :class:`CatchupDigest` to a JSON-serialisable dict."""
    from opshub.services.catchup import CatchupDigest

    assert isinstance(digest, CatchupDigest)

    def _iso(value: object) -> str | None:
        from datetime import datetime

        return value.isoformat() if isinstance(value, datetime) else None

    return {
        "since": _iso(digest.since),
        "advanced_to": _iso(digest.advanced_to),
        "new_sources_total": digest.new_sources_total,
        "new_sources": [
            {
                "id": s.id,
                "connector_name": s.connector_name,
                "source_type": s.source_type,
                "title": s.title,
                "url": s.url,
                "observed_at": _iso(s.observed_at),
            }
            for s in digest.new_sources
        ],
        "open_commitments_total": digest.open_commitments_total,
        "overdue_commitments_total": digest.overdue_commitments_total,
        "open_commitments": [
            {
                "id": c.id,
                "direction": c.direction,
                "counterparty": c.counterparty,
                "due": c.due,
                "text": c.text,
                "overdue": c.overdue,
            }
            for c in digest.open_commitments
        ],
        "new_demand_total": digest.new_demand_total,
        "new_demand": [
            {
                "team_id": d.team_id,
                "channel_id": d.channel_id,
                "channel_name": d.channel_name,
                "demand_kind": d.demand_kind,
                "last_demand_user_id": d.last_demand_user_id,
                "last_demand_excerpt": d.last_demand_excerpt,
                "last_demand_permalink": d.last_demand_permalink,
                "last_demand_at": _iso(d.last_demand_at),
            }
            for d in digest.new_demand
        ],
    }


def _render_text(digest: object) -> None:
    """Render a :class:`CatchupDigest` as a human-readable text digest."""
    from opshub.services.catchup import CatchupDigest

    assert isinstance(digest, CatchupDigest)

    since = "the beginning" if digest.since is None else digest.since.date().isoformat()
    typer.echo(f"Catchup since {since}:")

    # New sources.
    typer.echo("")
    typer.echo(f"New sources: {digest.new_sources_total}")
    for s in digest.new_sources:
        when = s.observed_at.date().isoformat()
        typer.echo(f"  [{when}] {s.connector_name}/{s.source_type}: {s.title}")

    # Open commitments (overdue surfaced first).
    typer.echo("")
    overdue = digest.overdue_commitments_total
    typer.echo(f"Open commitments: {digest.open_commitments_total} ({overdue} overdue)")
    for c in digest.open_commitments:
        arrow = "→ I owe" if c.direction == "i_owe" else "← owed to me"
        flag = "OVERDUE " if c.overdue else ""
        due = f" (due {c.due})" if c.due else ""
        party = f" [{c.counterparty}]" if c.counterparty else ""
        typer.echo(f"  {flag}{arrow}{party}{due}: {c.text}")

    # New Slack demand.
    typer.echo("")
    typer.echo(f"New Slack demand: {digest.new_demand_total}")
    for d in digest.new_demand:
        channel = d.channel_name or d.channel_id
        excerpt = d.last_demand_excerpt or ""
        typer.echo(f"  [{d.demand_kind} {channel}] {excerpt}")

    if digest.advanced_to is not None:
        typer.echo("")
        typer.echo(f"(seen marker advanced to {digest.advanced_to.isoformat()})")


def register(app: typer.Typer) -> None:
    """Register :func:`catchup_command` on the root Typer app.

    Mirrors :func:`opshub.cli.brief.register`: encapsulates the registration
    so :mod:`opshub.cli.app` only has to call ``register(app)``.
    """
    app.command(
        name="catchup",
        help="Surface what changed since you last caught up (read-only diff digest).",
    )(catchup_command)
