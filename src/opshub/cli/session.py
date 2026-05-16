"""``opshub session ...`` subcommands (Phase 2 step 6).

Three commands cover the work-session aggregate's user-facing surface:

* ``opshub session start [--scope ...] [--actor ...]`` — start a new
  session. Prints the session's 26-char ULID on stdout and writes it
  to the per-user state file (see
  :func:`opshub.cli._actor.set_current_session_id`) so subsequent
  commands auto-resolve ``--session``.
* ``opshub session end [<session-id>] [--summary ...] [--actor ...]``
  — end the session. With ``session_id`` omitted, the state file's
  current session is used (and cleared on success).
* ``opshub session list [--format ...]`` — print active sessions.
  Three output formats: ``table`` / ``json`` / ``md``.

Module-level imports stay limited to ``__future__`` + ``typer`` so
``opshub --help`` cold start stays under the ADR-0001 budget. Heavy
imports (SQLAlchemy, the service layer, owner resolution) load lazily
inside each callback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from opshub.services.work_session_service import WorkSessionRow


session_app = typer.Typer(name="session", help="Work session commands.", no_args_is_help=True)

# Inline rendering knobs (mirrors the lock CLI's stop-gap renderer).
_ALLOWED_FORMATS = ("table", "json", "md")
_ID_PREFIX_LEN = 8
_ACTOR_WIDTH = 20
_SCOPE_WIDTH = 24
_STARTED_AT_WIDTH = 19
_ELLIPSIS = "..."


@session_app.command("start")
def session_start(
    scope: str | None = typer.Option(
        None,
        "--scope",
        help="Optional free-form scope label (e.g. 'phase-2 step 6').",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Override the recorded actor (defaults to $OPSHUB_ACTOR or 'cli:default').",
    ),
) -> None:
    """Start a new work session and print its ULID.

    Stdout contains exactly one line: the 26-character ULID assigned
    to the new session. The ULID is also written to the per-user
    state file (``~/.local/state/opshub/current-session`` or
    ``$XDG_STATE_HOME/opshub/current-session``) so the next
    ``opshub agent run begin`` / ``opshub lock acquire`` auto-resolve
    the session.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._actor import resolve_owner, set_current_session_id
    from opshub.cli._wiring import build_session_service

    owner = resolve_owner(actor=actor)
    service = build_session_service(owner.actor)
    event = service.start(scope=scope)
    set_current_session_id(event.aggregate_id)
    typer.echo(event.aggregate_id)


@session_app.command("end")
def session_end(
    session_id: str | None = typer.Argument(
        None,
        help="Work session ULID to end. Defaults to the current state-file session.",
    ),
    summary: str | None = typer.Option(
        None,
        "--summary",
        help="Optional wrap-up summary recorded on the event.",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Override the recorded actor (defaults to $OPSHUB_ACTOR or 'cli:default').",
    ),
) -> None:
    """End the current (or specified) work session.

    When ``session_id`` is omitted the command consults the state file
    via :func:`opshub.cli._actor.get_current_session_id`. A missing
    state file with no explicit id surfaces as a non-zero CLI exit
    via the top-level :class:`ValidationError` handler.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._actor import (
        clear_current_session,
        get_current_session_id,
        resolve_owner,
    )
    from opshub.cli._wiring import build_session_service
    from opshub.core.errors import ValidationError

    resolved_id = session_id if session_id is not None else get_current_session_id()
    if resolved_id is None:
        raise ValidationError(
            "no session_id given and no active session recorded; "
            "run 'opshub session start' first or pass <session-id>"
        )
    owner = resolve_owner(actor=actor)
    service = build_session_service(owner.actor)
    service.end(session_id=resolved_id, summary=summary)
    # Only clear the state file when the ended session was actually
    # the one tracked there; ending a different session should not
    # disturb the active bracket.
    if resolved_id == get_current_session_id():
        clear_current_session()
    typer.echo(f"ended {resolved_id}")


@session_app.command("list")
def session_list(
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table | json | md.",
    ),
) -> None:
    """List active work sessions (``state == 'active'``)."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_session_service

    service = build_session_service(actor="cli:session-list")
    rows = service.list_active()
    typer.echo(_render_session_list(rows, fmt=fmt))


# --------------------------------------------------------------- rendering


def _render_session_list(rows: list[WorkSessionRow], *, fmt: str) -> str:
    """Render ``rows`` in ``fmt`` format (inline until ``_render`` lands)."""
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.core.errors import ValidationError

    if fmt not in _ALLOWED_FORMATS:
        raise ValidationError(
            f"invalid --format {fmt!r}; expected one of {', '.join(_ALLOWED_FORMATS)}"
        )
    if fmt == "json":
        return _render_session_list_json(rows)
    if fmt == "md":
        return _render_session_list_md(rows)
    return _render_session_list_table(rows)


def _render_session_list_json(rows: list[WorkSessionRow]) -> str:
    """Serialise rows as a JSON array. Datetimes use ISO 8601 format."""
    import json
    from dataclasses import asdict
    from datetime import datetime

    payload: list[dict[str, object]] = []
    for row in rows:
        as_dict: dict[str, object] = dict(asdict(row))
        for key, value in list(as_dict.items()):
            if isinstance(value, datetime):
                as_dict[key] = value.isoformat()
        payload.append(as_dict)
    return json.dumps(payload, ensure_ascii=False)


def _render_session_list_md(rows: list[WorkSessionRow]) -> str:
    """Render rows as a GitHub-flavoured Markdown table."""
    header = "| ID | Actor | Scope | Started |"
    separator = "| --- | --- | --- | --- |"
    if not rows:
        return "\n".join([header, separator])
    body_lines = [
        "| {id} | {actor} | {scope} | {started} |".format(
            id=row.id[:_ID_PREFIX_LEN],
            actor=_escape_md(row.actor),
            scope=_escape_md(row.scope or ""),
            started=_format_datetime(row.started_at),
        )
        for row in rows
    ]
    return "\n".join([header, separator, *body_lines])


def _render_session_list_table(rows: list[WorkSessionRow]) -> str:
    """Render rows as an aligned plain-text table."""
    header = (
        f"{'ID':<{_ID_PREFIX_LEN}}  "
        f"{'ACTOR':<{_ACTOR_WIDTH}}  "
        f"{'SCOPE':<{_SCOPE_WIDTH}}  "
        f"{'STARTED':<{_STARTED_AT_WIDTH}}"
    )
    if not rows:
        return header
    body_lines = [
        (
            f"{row.id[:_ID_PREFIX_LEN]:<{_ID_PREFIX_LEN}}  "
            f"{_truncate(row.actor, _ACTOR_WIDTH):<{_ACTOR_WIDTH}}  "
            f"{_truncate(row.scope or '', _SCOPE_WIDTH):<{_SCOPE_WIDTH}}  "
            f"{_format_datetime(row.started_at):<{_STARTED_AT_WIDTH}}"
        )
        for row in rows
    ]
    return "\n".join([header, *body_lines])


def _truncate(value: str, width: int) -> str:
    """Truncate ``value`` to ``width`` characters, appending ``...`` if cut."""
    if len(value) <= width:
        return value
    if width <= len(_ELLIPSIS):
        return value[:width]
    return value[: width - len(_ELLIPSIS)] + _ELLIPSIS


def _format_datetime(value: object) -> str:
    """Render a datetime as ``YYYY-MM-DD HH:MM:SS`` (UTC components)."""
    from datetime import datetime

    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    return str(value)


def _escape_md(value: str) -> str:
    """Escape pipe characters so cell content does not break the table."""
    return value.replace("|", "\\|")
