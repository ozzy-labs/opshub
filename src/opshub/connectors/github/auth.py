"""GitHub PAT resolution (Phase 3 step B1).

Token storage strategy per ADR-0014:

- Read from keyring under service ``"opshub"``, key
  ``"connector:github:pat"``.
- Env-var override: ``OPSHUB_CONNECTOR_GITHUB_PAT`` (handled by
  :func:`opshub.core.secrets.get_secret` — the same precedence rule
  applies to every connector token).

This module is intentionally tiny so the cold-start path remains lazy
(``keyring`` is in the ``[secrets]`` extras and is only imported when a
token is actually requested through :mod:`opshub.core.secrets`).
"""

from __future__ import annotations

from opshub.core.errors import ConfigError
from opshub.core.secrets import get_secret

_SECRET_KEY = "connector:github:pat"

__all__ = ["GITHUB_PAT_SECRET_KEY", "get_github_token"]

#: Keyring key used to store the GitHub PAT. Exposed so the CLI command
#: ``opshub connector auth set github`` writes to the same key the
#: connector reads at sync time — i.e. this constant is the contract
#: between the CLI writer and the connector reader.
GITHUB_PAT_SECRET_KEY = _SECRET_KEY


def get_github_token() -> str:
    """Return the configured GitHub PAT.

    Raises :class:`~opshub.core.errors.ConfigError` if no token is set
    in keyring and no ``OPSHUB_CONNECTOR_GITHUB_PAT`` env var is
    present. The error message points the user at the documented
    configuration paths so the failure is actionable.
    """
    token = get_secret(_SECRET_KEY)
    if token is None:
        raise ConfigError(
            "GitHub PAT is not configured; run "
            "`opshub connector auth set github` or set "
            "OPSHUB_CONNECTOR_GITHUB_PAT in the environment"
        )
    return token
