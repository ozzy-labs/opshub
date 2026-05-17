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

from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY, get_github_token
from opshub.core.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Iterator


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
