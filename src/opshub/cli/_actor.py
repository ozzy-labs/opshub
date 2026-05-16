"""Resolve actor + work_session_id for OpsHub commands.

Order of precedence:

1. Explicit CLI flag (``--actor`` / ``--session``)
2. Environment variable (``OPSHUB_ACTOR`` / ``OPSHUB_WORK_SESSION_ID``)
3. Default (``"cli:default"`` for actor; ``None`` for work_session_id)

The owner pair this module resolves is the value :class:`LockService`
stamps on every :class:`LockAcquired` event (ADR-0013, "Owner"). Step 6
will extend :func:`resolve_owner` (or add a sibling helper) with
state-file-backed ``current_session_id()`` so ``opshub session start``
auto-injects the session ULID; for now the helper only consults the
explicit flag / env var path.

Lazy-import discipline (ADR-0001): module-level imports stay limited to
``os`` and ``dataclasses`` so importing :mod:`opshub.cli._actor` is
effectively free for the ``opshub --help`` cold start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Owner", "resolve_owner"]

_ENV_ACTOR = "OPSHUB_ACTOR"
_ENV_WORK_SESSION_ID = "OPSHUB_WORK_SESSION_ID"
_DEFAULT_ACTOR = "cli:default"


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
    * ``work_session_id``: CLI flag → ``OPSHUB_WORK_SESSION_ID`` env var → ``None``.

    Empty strings in the env var are treated as "unset" so a stray
    ``OPSHUB_ACTOR=`` in a parent shell does not override the default.
    """
    resolved_actor = actor if actor is not None else _from_env(_ENV_ACTOR) or _DEFAULT_ACTOR
    resolved_session = (
        work_session_id if work_session_id is not None else _from_env(_ENV_WORK_SESSION_ID)
    )
    return Owner(actor=resolved_actor, work_session_id=resolved_session)


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
