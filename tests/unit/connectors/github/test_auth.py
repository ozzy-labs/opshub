"""Tests for ``opshub.connectors.github.auth`` (Phase 3 step B1).

``get_github_token`` is a thin wrapper around :func:`opshub.core.secrets.get_secret`
that adds two concrete behaviours on top:

1. A fixed keyring key (``"connector:github:pat"``) exposed via the
   :data:`GITHUB_PAT_SECRET_KEY` constant. This is the contract between
   the CLI writer (``opshub connector auth set github``) and the
   connector reader.
2. A friendly :class:`ConfigError` when the token is absent from both
   keyring and the ``OPSHUB_CONNECTOR_GITHUB_PAT`` env-var override,
   pointing the user at the documented configuration paths.

The keyring extras (``opshub[secrets]``) may not be installed in every
environment, so the backend-touching tests gate behind
``pytest.importorskip`` (same pattern as ``tests/unit/core/test_secrets``).
The env-var override path does not touch keyring at all and is exercised
unconditionally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from opshub.connectors.github.auth import (
    GITHUB_PAT_SECRET_KEY,
    get_github_token,
)
from opshub.connectors.github.auth import (
    test_token as github_test_token,
)
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


keyring: Any = pytest.importorskip(
    "keyring",
    reason="opshub.connectors.github.auth backend tests require the 'secrets' extras",
)
_KeyringBackend: Any = keyring.backend.KeyringBackend


class _InMemoryKeyring(_KeyringBackend):  # type: ignore[misc,unused-ignore]
    """Process-local keyring backend used by the test suite.

    Mirrors the helper in ``tests/unit/core/test_secrets`` so the
    contract between this module and ``opshub.core.secrets`` is
    exercised against the same backend shape.
    """

    priority = 1  # type: ignore[assignment,unused-ignore]

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


@pytest.fixture
def in_memory_keyring() -> Iterator[_InMemoryKeyring]:
    """Install an in-memory keyring backend for the duration of the test."""
    previous = keyring.get_keyring()
    backend = _InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


# ----- constants ---------------------------------------------------------


def test_github_pat_secret_key_constant() -> None:
    """The exported constant is the public contract — the CLI writer and the
    connector reader must agree on this exact key.

    Any change to this string is a breaking change for already-stored
    tokens (they would suddenly be unreadable), hence the explicit
    pin in a test.
    """
    assert GITHUB_PAT_SECRET_KEY == "connector:github:pat"


# ----- happy path: keyring ------------------------------------------------


def test_get_github_token_returns_from_keyring(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    """A token stored under the documented key is returned verbatim."""
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)
    in_memory_keyring.set_password("opshub", "connector:github:pat", "ghp_stored")

    assert get_github_token() == "ghp_stored"


# ----- env-var override (works without the secrets extras too) ----------


def test_get_github_token_returns_from_env_var(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    """``OPSHUB_CONNECTOR_GITHUB_PAT`` wins over an empty keyring.

    ADR-0014 documents the env-var override as the CI / docker / WSL2
    escape hatch — this test pins that contract.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_from_env")
    # Keyring is intentionally empty; the env var must be used.

    assert get_github_token() == "ghp_from_env"


# ----- error path: nothing configured -----------------------------------


def test_get_github_token_raises_config_error_when_unset(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    """Empty keyring + no env var → actionable ConfigError.

    The message must mention both the CLI command and the env-var
    override so the user can self-service.
    """
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        get_github_token()

    message = str(excinfo.value)
    assert "opshub connector auth set github" in message
    assert "OPSHUB_CONNECTOR_GITHUB_PAT" in message


# ----- test_token() (Phase 7.x — `opshub connector auth test`) ----------
#
# These tests inject an ``httpx.MockTransport``-backed client via the
# ``client=`` seam — mirrors the precedent set by
# ``tests/unit/connectors/github/test_api.py`` (which also lets callers
# pass a pre-configured Client). The DI seam keeps the contract honest:
# tests never reach the real api.github.com endpoint, and the
# production default-client construction path is exercised by the
# integration tests indirectly.


def _mock_github_client(
    handler: Callable[[Any], Any],
) -> Any:
    """Build an ``httpx.Client`` whose every request is matched against ``handler``.

    Mirrors :func:`tests.unit.connectors.github.test_api._client` —
    constructing the client with :class:`httpx.MockTransport` means
    the call into ``test_token`` follows the production code path
    (Client.get → response.json / headers) without hitting the
    network.
    """
    import httpx

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )


def test_test_token_success_returns_user_fields() -> None:
    """Happy path: ``GET /user`` returns the user payload + scopes header.

    The CLI ``connector auth test github`` consumer of this function
    pins ``login`` / ``name`` / ``scopes`` as its display contract.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        # Pin the request shape: Authorization header must carry the
        # Bearer token; Accept must request the GitHub JSON format.
        assert request.url.path == "/user"
        assert request.headers["Authorization"] == "Bearer ghp_test"
        assert request.headers["Accept"] == "application/vnd.github+json"
        return httpx.Response(
            200,
            json={"login": "alice", "name": "Alice Smith"},
            headers={"X-OAuth-Scopes": "repo, read:user"},
        )

    # ``client=`` injects a Client without the production default's
    # auth header (the production path bakes it in at construction).
    # We add the header here so the request-shape assertion above sees
    # it; this mirrors the api.py test seam.
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
        headers={
            "Authorization": "Bearer ghp_test",
            "Accept": "application/vnd.github+json",
        },
    )

    result = github_test_token(token="ghp_test", client=client)

    assert result == {
        "login": "alice",
        "name": "Alice Smith",
        "scopes": "repo, read:user",
    }


def test_test_token_handles_missing_name_field() -> None:
    """A user without a configured ``name`` (the field is ``None``) renders
    as empty string, not as the literal ``"None"`` repr. The CLI
    consumer turns empty values into ``(none)`` for readability — we
    pin the empty-string contract here."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"login": "bob", "name": None},
            headers={"X-OAuth-Scopes": ""},
        )

    result = github_test_token(token="ghp_test", client=_mock_github_client(handler))

    assert result["login"] == "bob"
    assert result["name"] == ""  # not "None"
    assert result["scopes"] == ""


def test_test_token_raises_when_unauthorized() -> None:
    """A 401 (revoked / wrong-scope PAT) surfaces as :class:`ConfigError`
    with the status code but NOT the response body — the body can echo
    rate-limit context that leaks Authorization bits per GitHub docs."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(ConfigError) as excinfo:
        github_test_token(token="ghp_invalid", client=_mock_github_client(handler))

    message = str(excinfo.value)
    assert "401" in message
    # Token-leak invariant: the PAT must NEVER appear in the error
    # message even when the server echoes it. Pin this so a future
    # refactor that includes ``response.text`` triggers a red CI.
    assert "ghp_invalid" not in message


def test_test_token_raises_on_transport_error() -> None:
    """DNS / TLS / connection errors must surface as :class:`ConfigError`
    with the exception *type name only* — the message can echo
    request body which carries the Authorization header."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS resolution failed: ghp_test")

    with pytest.raises(ConfigError) as excinfo:
        github_test_token(token="ghp_test", client=_mock_github_client(handler))

    message = str(excinfo.value)
    assert "ConnectError" in message
    # Token-leak invariant: even if the exception message embeds the
    # token (as it does here for the test fixture), the surfaced
    # ConfigError must NOT include it.
    assert "ghp_test" not in message
