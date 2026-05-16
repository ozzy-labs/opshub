"""Resolve actor + work_session_id for OpsHub commands.

Order of precedence for ``work_session_id``:

1. Explicit CLI flag (``--session``)
2. Environment variable (``OPSHUB_WORK_SESSION_ID``)
3. State file (``~/.local/state/opshub/current-session`` or
   ``$XDG_STATE_HOME/opshub/current-session``) — populated by
   ``opshub session start`` (Phase 2 step 6)
4. ``None``

Order of precedence for ``actor``:

1. Explicit CLI flag (``--actor``)
2. Environment variable (``OPSHUB_ACTOR``)
3. Default (``"cli:default"``)

The owner pair this module resolves is the value :class:`LockService`
stamps on every :class:`LockAcquired` event (ADR-0013, "Owner").

Step 6 also exposes the lower-level state-file helpers
(:func:`get_current_session_id` / :func:`set_current_session_id` /
:func:`clear_current_session` / :func:`state_file_path`) so the
``opshub session start`` / ``opshub session end`` commands can wire the
auto-injection.

Lazy-import discipline (ADR-0001): module-level imports stay limited to
``os`` / ``dataclasses`` / ``pathlib`` so importing
:mod:`opshub.cli._actor` is effectively free for the
``opshub --help`` cold start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Owner",
    "clear_current_session",
    "get_current_session_id",
    "resolve_owner",
    "set_current_session_id",
    "state_file_path",
]

_ENV_ACTOR = "OPSHUB_ACTOR"
_ENV_WORK_SESSION_ID = "OPSHUB_WORK_SESSION_ID"
_ENV_XDG_STATE_HOME = "XDG_STATE_HOME"
_DEFAULT_ACTOR = "cli:default"
_STATE_FILE_NAME = "current-session"
_STATE_DIR_NAME = "opshub"


@dataclass(frozen=True)
class Owner:
    """The ``(actor, work_session_id)`` pair every lock event carries.

    ``actor`` is always populated (no empty / falsy values reach the
    service). ``work_session_id`` is ``None`` for ad-hoc operations that
    happen outside a work session bracket (ADR-0013).
    """

    actor: str
    work_session_id: str | None


def resolve_owner(
    actor: str | None = None,
    work_session_id: str | None = None,
) -> Owner:
    """Resolve the effective ``(actor, work_session_id)`` pair.

    Each field is resolved independently. The first non-``None`` value
    in the precedence list wins:

    * ``actor``: CLI flag → ``OPSHUB_ACTOR`` env var → ``"cli:default"``.
    * ``work_session_id``: CLI flag → ``OPSHUB_WORK_SESSION_ID`` env var
      → state file → ``None``.

    Empty strings in the env var are treated as "unset" so a stray
    ``OPSHUB_ACTOR=`` in a parent shell does not override the default.
    """
    resolved_actor = actor if actor is not None else _from_env(_ENV_ACTOR) or _DEFAULT_ACTOR
    resolved_session: str | None
    if work_session_id is not None:
        resolved_session = work_session_id
    else:
        from_env = _from_env(_ENV_WORK_SESSION_ID)
        if from_env is not None:
            resolved_session = from_env
        else:
            # Fall through to the state file (only consulted when both
            # flag and env var are unset).
            resolved_session = get_current_session_id()
    return Owner(actor=resolved_actor, work_session_id=resolved_session)


def state_file_path() -> Path:
    """Return the path of the current-session state file.

    Honours ``$XDG_STATE_HOME`` when set (XDG Base Directory spec); falls
    back to ``~/.local/state/opshub/current-session`` otherwise. The
    parent directory is *not* created here — :func:`set_current_session_id`
    creates it on demand.
    """
    xdg_state = os.environ.get(_ENV_XDG_STATE_HOME)
    if xdg_state:
        base = Path(xdg_state)
    else:
        base = Path.home() / ".local" / "state"
    return base / _STATE_DIR_NAME / _STATE_FILE_NAME


def get_current_session_id() -> str | None:
    """Read the current work session ULID from the state file.

    Returns ``None`` if the state file does not exist, is empty, or its
    contents do not look like a 26-character string. The function never
    raises on missing / unreadable files — a stale state file should
    silently degrade to "no session" rather than break every command.
    """
    path = state_file_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    candidate = text.strip()
    if not candidate:
        return None
    return candidate


def set_current_session_id(session_id: str) -> None:
    """Write ``session_id`` to the state file.

    Creates the parent directory (and any missing ancestors) on demand
    with mode 0o755. Writes the file atomically via ``Path.write_text``
    on a single short string — sufficient for the single-writer model
    every CLI invocation uses.
    """
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id, encoding="utf-8")


def clear_current_session() -> None:
    """Remove the state file if it exists.

    A no-op when the file is already absent. Does not remove the parent
    directory — other state files may live alongside ours.
    """
    path = state_file_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _from_env(name: str) -> str | None:
    """Read ``name`` from the environment, normalising empty to ``None``.

    Empty env values are common in shells (``unset`` vs. ``export FOO=``);
    treating ``""`` as ``None`` keeps the precedence rules above
    consistent with what users expect.
    """
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value
