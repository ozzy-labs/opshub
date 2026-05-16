"""``opshub lock ...`` subcommands (Phase 2 step 5, ADR-0013).

Three commands cover the lock aggregate's user-facing surface:

* ``opshub lock acquire <scope> [--actor ...] [--session ...]`` — acquire
  a lock for ``(actor, work_session_id)``. Prints the lock's 26-char
  ULID on stdout so callers can pipe it into ``opshub lock release``.
* ``opshub lock release <lock-id> [--actor ...] [--session ...]`` —
  release a lock the caller owns. ``OwnershipError`` (different actor
  or different session) surfaces as a non-zero exit; the lock stays
  held.
* ``opshub lock list [--scope-type ...] [--format ...]`` — print active
  locks, optionally filtered by ``scope_type``. Three output formats
  (``table`` / ``json`` / ``md``) match :mod:`opshub.cli._task_list`
  conventions until step 3 lands ``cli/_render``; the inline renderer
  here will collapse into a call to that shared module then.

Module-level imports stay limited to ``__future__`` + ``typer`` so
``opshub --help`` cold start stays under the ADR-0001 budget. Heavy
imports (SQLAlchemy, the service layer, owner resolution) load lazily
inside each callback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from opshub.services.lock_service import LockRow

# Heavy imports happen inside command bodies (ADR-0001 lazy-import rule).

lock_app = typer.Typer(name="lock", help="Coordination locks.", no_args_is_help=True)

# Inline rendering knobs — see module docstring for the step 3 plan.
_ALLOWED_FORMATS = ("table", "json", "md")
_ID_PREFIX_LEN = 8
_SCOPE_TYPE_WIDTH = 8  # "project"
_SCOPE_ID_WIDTH = 16
_ACTOR_WIDTH = 20
_ACQUIRED_AT_WIDTH = 19  # "YYYY-MM-DD HH:MM:SS"
_ELLIPSIS = "..."


@lock_app.command("acquire")
def lock_acquire(
    scope: str = typer.Argument(
        ...,
        help="Lock scope: 'task:<ulid>' or 'global:' (project: is reserved).",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Override the recorded actor (defaults to $OPSHUB_ACTOR or 'cli:default').",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help=(
            "Bind the lock to a work session ULID (defaults to $OPSHUB_WORK_SESSION_ID or none)."
        ),
    ),
) -> None:
    """Acquire ``scope`` and print the new lock's ULID.

    The lock event's ``aggregate_id`` is what gets echoed on stdout so
    callers can pipe it into ``opshub lock release``. Idempotent
    reacquire (same owner, same scope) returns the original lock ULID
    without appending a new event (ADR-0013).
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._actor import resolve_owner
    from opshub.cli._wiring import build_lock_service

    owner = resolve_owner(actor=actor, work_session_id=session)
    service = build_lock_service(owner.actor)
    event = service.acquire(scope, work_session_id=owner.work_session_id)
    typer.echo(event.aggregate_id)


@lock_app.command("release")
def lock_release(
    lock_id: str = typer.Argument(..., help="Lock ULID to release."),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Override the recorded actor (defaults to $OPSHUB_ACTOR or 'cli:default').",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help=("Bind to a work session ULID (defaults to $OPSHUB_WORK_SESSION_ID or none)."),
    ),
) -> None:
    """Release ``lock_id`` and print a confirmation.

    Owner identity is the ``(actor, work_session_id)`` pair from
    :func:`opshub.cli._actor.resolve_owner`. Mismatch on either component
    raises :class:`OwnershipError`; missing / already-released locks
    raise :class:`NotFoundError`. Both surface as non-zero CLI exits via
    the :func:`opshub.cli.app.main` wrapper.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._actor import resolve_owner
    from opshub.cli._wiring import build_lock_service

    owner = resolve_owner(actor=actor, work_session_id=session)
    service = build_lock_service(owner.actor)
    event = service.release(lock_id, work_session_id=owner.work_session_id)
    typer.echo(f"released {event.lock_id}")


@lock_app.command("list")
def lock_list(
    scope_type: str | None = typer.Option(
        None,
        "--scope-type",
        help="Filter by scope_type (task | project | global).",
    ),
    fmt: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table | json | md.",
    ),
) -> None:
    """List active locks (``released_at IS NULL``).

    Rendering is inlined here for Phase 2 step 5 because the shared
    ``cli/_render`` module from step 3 has not landed yet. When it does,
    this callback drops to a two-liner that calls into the shared
    renderer with a :class:`Column` descriptor list.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001).
    from opshub.cli._wiring import build_lock_service

    # The list view uses the lock service for its DB binding; ``actor``
    # is not stamped on any event here so the default keeps the wiring
    # uniform.
    service = build_lock_service(actor="cli:lock-list")
    rows = service.list_active(scope_type=scope_type)
    typer.echo(_render_lock_list(rows, fmt=fmt))


# --------------------------------------------------------------- rendering


def _render_lock_list(rows: list[LockRow], *, fmt: str) -> str:
    """Render ``rows`` in ``fmt`` format (inline until step 3 lands ``_render``).

    Mirrors :func:`opshub.cli._task_list.render_task_list` structurally so
    the migration to a shared renderer is mechanical.
    """
    # Lazy imports: keep CLI cold start fast (ADR-0001). ``typer`` /
    # ``__future__`` plus this module's own constants are the only
    # top-level imports.
    from opshub.core.errors import ValidationError

    if fmt not in _ALLOWED_FORMATS:
        raise ValidationError(
            f"invalid --format {fmt!r}; expected one of {', '.join(_ALLOWED_FORMATS)}"
        )
    if fmt == "json":
        return _render_lock_list_json(rows)
    if fmt == "md":
        return _render_lock_list_md(rows)
    return _render_lock_list_table(rows)


def _render_lock_list_json(rows: list[LockRow]) -> str:
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


def _render_lock_list_md(rows: list[LockRow]) -> str:
    """Render rows as a GitHub-flavoured Markdown table."""
    header = "| ID | Scope | Actor | Acquired |"
    separator = "| --- | --- | --- | --- |"
    if not rows:
        return "\n".join([header, separator])
    body_lines = [
        "| {id} | {scope} | {actor} | {acquired} |".format(
            id=row.id[:_ID_PREFIX_LEN],
            scope=_escape_md(f"{row.scope_type}:{row.scope_id}"),
            actor=_escape_md(row.actor),
            acquired=_format_datetime(row.acquired_at),
        )
        for row in rows
    ]
    return "\n".join([header, separator, *body_lines])


def _render_lock_list_table(rows: list[LockRow]) -> str:
    """Render rows as an aligned plain-text table."""
    header = (
        f"{'ID':<{_ID_PREFIX_LEN}}  "
        f"{'SCOPE_TYPE':<{_SCOPE_TYPE_WIDTH}}  "
        f"{'SCOPE_ID':<{_SCOPE_ID_WIDTH}}  "
        f"{'ACTOR':<{_ACTOR_WIDTH}}  "
        f"{'ACQUIRED':<{_ACQUIRED_AT_WIDTH}}"
    )
    if not rows:
        return header
    body_lines = [
        (
            f"{row.id[:_ID_PREFIX_LEN]:<{_ID_PREFIX_LEN}}  "
            f"{row.scope_type:<{_SCOPE_TYPE_WIDTH}}  "
            f"{_truncate(row.scope_id, _SCOPE_ID_WIDTH):<{_SCOPE_ID_WIDTH}}  "
            f"{_truncate(row.actor, _ACTOR_WIDTH):<{_ACTOR_WIDTH}}  "
            f"{_format_datetime(row.acquired_at):<{_ACQUIRED_AT_WIDTH}}"
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
    """Render a datetime as ``YYYY-MM-DD HH:MM:SS`` (UTC components).

    SQLite returns ``DateTime(timezone=True)`` columns as naive datetimes
    whose components reflect UTC; we accept both flavours.
    """
    from datetime import datetime

    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    return str(value)


def _escape_md(value: str) -> str:
    """Escape pipe characters so cell content does not break the table."""
    return value.replace("|", "\\|")
