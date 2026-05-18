"""GitHub PAT resolution (Phase 3 step B1; verification API added in Phase 7.x).

Token storage strategy per ADR-0014:

- Read from keyring under service ``"opshub"``, key
  ``"connector:github:pat"``.
- Env-var override: ``OPSHUB_CONNECTOR_GITHUB_PAT`` (handled by
  :func:`opshub.core.secrets.get_secret` — the same precedence rule
  applies to every connector token).

This module is intentionally tiny so the cold-start path remains lazy
(``keyring`` is in the ``[secrets]`` extras and is only imported when a
token is actually requested through :mod:`opshub.core.secrets`).

:func:`test_token` (added in Phase 7.x for the ``opshub connector auth
test`` CLI) calls GitHub's ``GET /user`` endpoint to verify that the
PAT is valid. ``httpx`` is imported lazily inside the function so the
cold-start budget (ADR-0001) is preserved — operators on the
auth-only path never pay the import cost. The HTTP client follows the
same dependency-injection pattern as :mod:`opshub.connectors.github.api`
(``client: httpx.Client | None = None``) so tests can inject an
``httpx.MockTransport``-backed client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opshub.core.errors import ConfigError
from opshub.core.secrets import get_secret

if TYPE_CHECKING:
    import httpx

_SECRET_KEY = "connector:github:pat"

__all__ = ["GITHUB_PAT_SECRET_KEY", "get_github_token", "test_token"]

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


def test_token(
    *,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, str]:
    """Verify a GitHub PAT by calling ``GET /user``.

    Returns a dict containing ``login`` (the GitHub username),
    ``name`` (display name, may be empty for users without a configured
    name), and ``scopes`` (comma-separated list extracted from the
    ``X-OAuth-Scopes`` response header — empty string if GitHub omits
    the header for fine-grained PATs).

    Parameters
    ----------
    token:
        Optional explicit token. When omitted resolves via
        :func:`get_github_token` (keyring + env-var override). The
        explicit form is the documented seam for unit tests so they
        do not need to touch the real keyring.
    client:
        Optional pre-configured :class:`httpx.Client` (the documented
        seam for tests — pass a client built with
        ``httpx.MockTransport`` to assert against without hitting the
        real GitHub API). When ``None`` a default client is created
        with the Bearer auth header baked in; the client is closed at
        the end of the call. When supplied, the caller owns the
        lifecycle and must have already set the Authorization header.
        Mirrors the pattern used by :mod:`opshub.connectors.github.api`.

    Raises
    ------
    ConfigError
        On any network failure or non-2xx response. The PAT never
        appears in raised exceptions — only the exception type name
        (transport errors) or HTTP status code (API errors) surface,
        matching the Slack / Box token-leak invariant.
    """
    if token is None:
        token = get_github_token()

    # Lazy import keeps httpx off the cold-start path. The
    # ``connectors-github`` extras ship httpx; if it is missing this
    # branch never runs (operator never set up github connector).
    try:
        import httpx as _httpx
    except ImportError as exc:
        raise ConfigError(
            "GitHub support requires the [connectors-github] extras; "
            "install with `uv sync --extra connectors-github`"
        ) from exc

    owns_client = client is None
    if client is None:
        client = _httpx.Client(
            base_url="https://api.github.com",
            timeout=10.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "opshub-connector/0.1",
            },
        )
    try:
        try:
            response = client.get("/user")
        except Exception as exc:
            # Transport-level failures (DNS / TLS / connection) surface
            # as the exception type name only — never the message,
            # which can echo the request body and therefore the
            # Authorization header.
            raise ConfigError(f"GitHub auth.test failed: {type(exc).__name__}") from exc

        if response.status_code != 200:
            # ``response.text`` can echo the rejected token in rare
            # cases (GitHub error envelopes sometimes include the
            # rate-limit context which has been observed to leak
            # Authorization bits in the wild). Surface only the status
            # code, not the body.
            raise ConfigError(f"GitHub auth.test returned non-2xx: status={response.status_code}")

        payload: dict[str, Any] = response.json()
        # ``X-OAuth-Scopes`` is omitted for fine-grained PATs; default
        # to empty so the CLI shows ``scopes: (none reported)`` rather
        # than a KeyError. ``response.headers`` is a case-insensitive
        # multidict so both header-case variants work.
        scopes_raw = response.headers.get("X-OAuth-Scopes") or ""
        return {
            "login": str(payload.get("login", "")),
            "name": str(payload.get("name") or ""),
            "scopes": scopes_raw.strip(),
        }
    finally:
        if owns_client:
            client.close()
