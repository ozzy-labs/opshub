"""Tests for ``opshub connector auth set`` (Phase 3 step B1).

The CLI wraps :func:`opshub.core.secrets.set_secret` with three pieces
of behaviour worth pinning:

1. The ``--token`` flag round-trips into the same keyring key the
   connector reader uses (``GITHUB_PAT_SECRET_KEY``).
2. When ``--token`` is omitted the user is prompted with hidden input
   (we exercise the prompt path via ``CliRunner.invoke(..., input=...)``).
3. Empty / whitespace-only tokens and unknown connector names exit 2
   with a helpful stderr message — exit 0 on bad input would silently
   store an empty token.

By design there is **no** ``auth get`` command (we never echo tokens to
stdout); this test file pins that policy alongside the positive
behaviour. The in-memory keyring backend mirrors the pattern from
``tests/unit/core/test_secrets``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY
from opshub.core.secrets import get_secret

if TYPE_CHECKING:
    from collections.abc import Iterator


keyring: Any = pytest.importorskip(
    "keyring",
    reason="`opshub connector auth set` tests require the 'secrets' extras",
)
_KeyringBackend: Any = keyring.backend.KeyringBackend


class _InMemoryKeyring(_KeyringBackend):  # type: ignore[misc,unused-ignore]
    """Process-local keyring backend used by the test suite."""

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
def in_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[_InMemoryKeyring]:
    """Install an in-memory keyring backend and clear the env-var override.

    The env var is cleared here so ``get_secret`` (used to verify the
    round-trip) actually consults the in-memory backend instead of
    returning a leaked env value from an earlier test.
    """
    monkeypatch.delenv("OPSHUB_CONNECTOR_GITHUB_PAT", raising=False)
    previous = keyring.get_keyring()
    backend = _InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


# ----- happy paths -------------------------------------------------------


def test_auth_set_github_with_token_flag_stores_to_keyring(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``--token`` writes to the key the connector reader uses."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", "ghp_xxx"])

    assert result.exit_code == 0, result.stdout
    assert "github" in result.stdout
    # The connector reads the token via this exact key; the round-trip
    # check pins the CLI writer / connector reader contract.
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"


def test_auth_set_github_with_stdin_prompt(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """No ``--token`` → prompt; CliRunner.input feeds the hidden prompt."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "auth", "set", "github"],
        input="ghp_prompted\n",
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_prompted"


def test_auth_set_strips_surrounding_whitespace(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """Tokens pasted with trailing newline / spaces should be stored clean.

    Users routinely copy a PAT from a web UI and the clipboard picks up
    a trailing newline; silently storing ``"ghp_xxx\\n"`` would cause
    confusing 401s at sync time. The CLI normalises by stripping.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", "  ghp_xxx  "])

    assert result.exit_code == 0, result.stdout
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"


# ----- error paths -------------------------------------------------------


def test_auth_set_rejects_empty_token(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """``--token ""`` is a user error, not a "stored empty token"."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", ""])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    # Nothing was written to the keyring backend.
    assert get_secret(GITHUB_PAT_SECRET_KEY) is None


def test_auth_set_rejects_whitespace_only_token(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """Whitespace-only tokens are also rejected — stored empty token would
    be just as broken at sync time as outright empty input."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "github", "--token", "   "])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(GITHUB_PAT_SECRET_KEY) is None


def test_auth_set_unknown_connector(
    in_memory_keyring: _InMemoryKeyring,
) -> None:
    """Unknown connector → exit 2 with the supported list on stderr.

    Phase 3 ships only ``github``; Slack / MS365 / Box land in Phase 3.x.
    The error must say so explicitly instead of failing silently.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "set", "slack", "--token", "x"])

    assert result.exit_code == 2
    assert "unknown connector" in result.stderr
    assert "slack" in result.stderr
    assert "github" in result.stderr  # the supported-list hint


def test_auth_does_not_expose_get_subcommand() -> None:
    """Security policy: there is no ``auth get`` (tokens must not echo to stdout).

    Pinning this in a test means a future contributor who innocently
    adds ``@auth_app.command("get")`` to "round out the CRUD" will see
    a red CI before the change ships.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "get", "github"])
    # Typer reports unknown subcommands with exit code != 0; the precise
    # code is 2 for usage errors. The important assertion is that the
    # command did NOT succeed.
    assert result.exit_code != 0
