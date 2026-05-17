"""SaaS token storage (ADR-0014).

Thin wrapper around the ``keyring`` library so connectors can fetch /
store credentials uniformly. Module-level imports of ``keyring`` are
lazy (inside each function body) for two reasons:

1. ``keyring`` is in the ``[secrets]`` extras and may not be installed
   for users who only use ``opshub task`` etc. We want
   ``import opshub.core.secrets`` to succeed even without the extras,
   surfacing the missing dependency only when a secret operation is
   actually attempted.
2. The CLI's M6 cold-start guard whitelists ``opshub.core`` imports
   in ``cli/*.py``; keeping heavy ``keyring`` import deferred keeps
   the cold-start budget intact.

Environment-variable override:

- For each secret key like ``connector:github:pat`` the resolver also
  checks ``OPSHUB_CONNECTOR_GITHUB_PAT`` (uppercase, ``:`` → ``_``).
  Env var wins over keyring so CI / docker / WSL2 (where the OS
  keychain may not be reachable) can inject tokens without keyring
  setup.

Functions raise :class:`opshub.core.errors.ConfigError` on missing
extras or backend failures so callers see a uniform error type.
"""

from __future__ import annotations

import os
from typing import Any

from opshub.core.errors import ConfigError

__all__ = ["delete_secret", "get_secret", "set_secret"]

_SERVICE_NAME = "opshub"


def _env_var_name(key: str) -> str:
    """``"connector:github:pat"`` → ``"OPSHUB_CONNECTOR_GITHUB_PAT"``."""
    return "OPSHUB_" + key.replace(":", "_").replace("-", "_").upper()


def get_secret(key: str) -> str | None:
    """Return the secret for ``key``, or ``None`` if absent.

    Order: env var override → keyring backend.
    """
    env = os.environ.get(_env_var_name(key))
    if env is not None:
        return env
    backend = _import_keyring()
    try:
        value: str | None = backend.get_password(_SERVICE_NAME, key)
    except Exception as exc:  # keyring errors are surfaced uniformly
        raise ConfigError(f"keyring backend failed to read {key!r}: {exc}") from exc
    return value


def set_secret(key: str, value: str) -> None:
    """Store ``value`` for ``key`` in the keyring backend.

    Env-var override is read-only: this does NOT write to the
    environment. The env var is purely a runtime override for testing
    / CI.
    """
    backend = _import_keyring()
    try:
        backend.set_password(_SERVICE_NAME, key, value)
    except Exception as exc:
        raise ConfigError(f"keyring backend failed to store {key!r}: {exc}") from exc


def delete_secret(key: str) -> None:
    """Remove ``key`` from the keyring backend.

    Idempotent if the key was never set (the underlying backend raises
    ``keyring.errors.PasswordDeleteError`` which we swallow as no-op).
    """
    backend = _import_keyring()
    try:
        backend.delete_password(_SERVICE_NAME, key)
    except Exception:  # non-existent key is fine
        pass


def _import_keyring() -> Any:
    """Return the keyring module, with a clear error if extras are missing."""
    try:
        import keyring  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise ConfigError(
            "secrets storage requires the 'keyring' extras: "
            "uv pip install 'opshub[secrets]' "
            "(or set the OPSHUB_<KEY> env var override)"
        ) from exc
    return keyring
