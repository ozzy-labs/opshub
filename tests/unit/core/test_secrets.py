"""Tests for opshub.core.secrets (ADR-0014).

The keyring extras may or may not be installed. To keep these tests
hermetic (and to avoid touching the real OS keychain), we install an
in-memory ``keyring`` backend per test via ``keyring.set_keyring()``.
The env-var override path does not touch keyring at all and is
exercised independently.

The ``keyring`` package lives in the ``secrets`` optional-dependency,
which is *not* installed by ``just ci`` / ``uv sync --extra dev``. We
therefore gate the entire backend-touching section behind
``pytest.importorskip`` and skip the file when keyring is absent. The
static-analysis ignore comments allow pyright + mypy to type-check the
file in both states (extras installed / not installed).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest

from opshub.core import secrets as secrets_module
from opshub.core.errors import ConfigError
from opshub.core.secrets import delete_secret, get_secret, set_secret

if TYPE_CHECKING:
    from collections.abc import Iterator


keyring: Any = pytest.importorskip(
    "keyring",
    reason="opshub.core.secrets requires the 'secrets' extras for backend tests",
)
# ``keyring.backend`` / ``keyring.errors`` are referenced as attributes off the
# already-imported ``keyring`` module (resolved at runtime). Going through the
# attribute path avoids ``from keyring.backend import ...`` which would trip
# both ruff's I001 (importorskip must come first) and E402 (module-level import
# after statement) and require multiple noqa directives.
_KeyringBackend: Any = keyring.backend.KeyringBackend
_PasswordDeleteError: type[Exception] = keyring.errors.PasswordDeleteError


class _InMemoryKeyring(_KeyringBackend):  # type: ignore[misc,unused-ignore]
    """Process-local keyring backend used by the test suite.

    Replaces the real OS keychain so tests do not leak state across
    machines / CI runs.
    """

    priority = 1  # type: ignore[assignment,unused-ignore]

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}
        self.get_calls: list[tuple[str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.get_calls.append((service, username))
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError as exc:
            raise _PasswordDeleteError(f"no such password: {service!r}/{username!r}") from exc


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


# ----- env-var name conversion -------------------------------------------


def test_env_var_name_conversion() -> None:
    """``connector:github:pat`` → ``OPSHUB_CONNECTOR_GITHUB_PAT``.

    The conversion rule is part of the public override contract documented
    in ADR-0014.
    """
    # Accessing the private helper is intentional: the conversion rule is part
    # of the public override contract documented in ADR-0014, but exposing the
    # helper itself in ``__all__`` would invite ad-hoc external use.
    _env_var_name = secrets_module._env_var_name  # pyright: ignore[reportPrivateUsage]
    assert _env_var_name("connector:github:pat") == "OPSHUB_CONNECTOR_GITHUB_PAT"
    assert _env_var_name("connector:ms-graph:pat") == "OPSHUB_CONNECTOR_MS_GRAPH_PAT"
    assert _env_var_name("simple") == "OPSHUB_SIMPLE"


# ----- env-var override --------------------------------------------------


def test_env_var_override_takes_precedence(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    """Env var beats keyring AND keyring is not consulted."""
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "env-token")
    # Pre-populate keyring with a different value to prove env wins.
    in_memory_keyring.set_password("opshub", "connector:github:pat", "keyring-token")

    assert get_secret("connector:github:pat") == "env-token"
    # Crucial: get_password must NOT have been called.
    assert in_memory_keyring.get_calls == []


def test_get_secret_returns_none_for_unknown_key(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)
    assert get_secret("connector:github:pat") is None
    # Keyring was consulted exactly once.
    assert in_memory_keyring.get_calls == [("opshub", "connector:github:pat")]


# ----- set / get / delete round-trip -------------------------------------


def test_set_secret_writes_to_keyring(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)

    set_secret("connector:github:pat", "stored-token")
    assert get_secret("connector:github:pat") == "stored-token"


def test_delete_secret_round_trip(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)
    set_secret("connector:github:pat", "stored-token")
    assert get_secret("connector:github:pat") == "stored-token"

    delete_secret("connector:github:pat")
    assert get_secret("connector:github:pat") is None


def test_delete_secret_is_idempotent_for_missing_key(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``delete_secret`` on an absent key must not raise."""
    delete_secret("connector:nonexistent:pat")  # no exception


# ----- backend / extras failure paths ------------------------------------


def test_get_secret_wraps_backend_errors_in_config_error(
    monkeypatch: pytest.MonkeyPatch, in_memory_keyring: _InMemoryKeyring
) -> None:
    """A keyring backend that raises should surface as ``ConfigError``."""
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)

    def _boom(service: str, username: str) -> str | None:
        raise RuntimeError("backend down")

    monkeypatch.setattr(in_memory_keyring, "get_password", _boom)

    with pytest.raises(ConfigError, match="keyring backend failed to read"):
        get_secret("connector:github:pat")


def test_set_secret_wraps_backend_errors_in_config_error(
    in_memory_keyring: _InMemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(service: str, username: str, password: str) -> None:
        raise RuntimeError("backend down")

    monkeypatch.setattr(in_memory_keyring, "set_password", _boom)

    with pytest.raises(ConfigError, match="keyring backend failed to store"):
        set_secret("connector:github:pat", "v")


def test_missing_keyring_extras_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``keyring`` cannot be imported, callers see a helpful ConfigError.

    We simulate the "extras not installed" state by setting
    ``sys.modules["keyring"] = None``, which makes Python raise
    ``ImportError`` on the next ``import keyring`` (see CPython docs on
    sys.modules sentinel values).
    """
    monkeypatch.delenv("OPSHUB_MISSING_EXTRA_TEST", raising=False)
    # Force a fresh import to fail.
    monkeypatch.setitem(sys.modules, "keyring", None)

    with pytest.raises(ConfigError, match="requires the 'keyring' extras"):
        # No env var override → must reach _import_keyring().
        get_secret("missing:extra:test")
