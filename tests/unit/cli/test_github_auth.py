"""Tests for ``opshub github auth set / test`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors.github.auth import GITHUB_PAT_SECRET_KEY
from opshub.core.secrets import get_secret
from tests.unit.cli.conftest import InMemoryKeyring


def test_github_auth_set_with_token_flag_stores_to_keyring(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``--token`` writes to the key the connector reader uses."""
    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "set", "--token", "ghp_xxx"])

    assert result.exit_code == 0, result.stdout
    assert "github" in result.stdout
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"


def test_github_auth_set_with_stdin_prompt(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """No ``--token`` → prompt; CliRunner.input feeds the hidden prompt."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["github", "auth", "set"],
        input="ghp_prompted\n",
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_prompted"


def test_github_auth_set_strips_surrounding_whitespace(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Tokens pasted with trailing newline / spaces should be stored clean."""
    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "set", "--token", "  ghp_xxx  "])

    assert result.exit_code == 0, result.stdout
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"


def test_github_auth_set_rejects_empty_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``--token ""`` is a user error, not a "stored empty token"."""
    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "set", "--token", ""])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(GITHUB_PAT_SECRET_KEY) is None


def test_github_auth_set_rejects_whitespace_only_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Whitespace-only tokens are also rejected."""
    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "set", "--token", "   "])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(GITHUB_PAT_SECRET_KEY) is None


def test_github_pat_env_var_name_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 17-B keeps ``OPSHUB_CONNECTOR_GITHUB_PAT`` env var unchanged."""
    monkeypatch.setenv("OPSHUB_CONNECTOR_GITHUB_PAT", "ghp_env_wins")
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_env_wins"


# ----- auth test happy path -----------------------------------------------


def test_github_auth_test_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """``github auth test`` calls ``github.auth.test_token`` and renders aligned output."""
    from opshub.connectors.github import auth as github_auth

    def fake_test_token() -> dict[str, str]:
        return {"login": "alice", "name": "Alice Smith", "scopes": "repo, read:user"}

    monkeypatch.setattr(github_auth, "test_token", fake_test_token)

    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "test"])

    assert result.exit_code == 0, result.stdout
    assert "connector: github" in result.stdout
    assert "status:    ok" in result.stdout
    assert "alice" in result.stdout
    assert "Alice Smith" in result.stdout
    assert "repo, read:user" in result.stdout


def test_github_auth_test_failure_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ConfigError`` from the connector surfaces as exit code 1 + status: failed."""
    from opshub.connectors.github import auth as github_auth
    from opshub.core.errors import ConfigError

    def fake_test_token() -> dict[str, str]:
        raise ConfigError("GitHub auth.test returned non-2xx: status=401")

    monkeypatch.setattr(github_auth, "test_token", fake_test_token)

    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "test"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "401" in result.stderr


def test_github_auth_test_renders_empty_values_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-string values render as ``(none)``."""
    from opshub.connectors.github import auth as github_auth

    def fake_test_token() -> dict[str, str]:
        return {"login": "bob", "name": "", "scopes": ""}

    monkeypatch.setattr(github_auth, "test_token", fake_test_token)

    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "test"])

    assert result.exit_code == 0
    assert "(none)" in result.stdout


def test_github_auth_does_not_expose_get_subcommand() -> None:
    """Security policy: no ``auth get``."""
    runner = CliRunner()
    result = runner.invoke(app, ["github", "auth", "get"])
    assert result.exit_code != 0


# ----- keyring slot separation --------------------------------------------


def test_github_and_slack_use_distinct_keyring_slots(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """GitHub PAT and Slack token live under different keyring slots."""
    pytest.importorskip("slack_sdk", reason="Slack extras required")
    from opshub.connectors.slack.auth import slack_token_secret_key

    runner = CliRunner()
    r1 = runner.invoke(app, ["github", "auth", "set", "--token", "ghp_xxx"])
    r2 = runner.invoke(app, ["slack", "auth", "set", "--workspace", "acme", "--token", "xoxp-yyy"])

    assert r1.exit_code == 0
    assert r2.exit_code == 0
    assert get_secret(GITHUB_PAT_SECRET_KEY) == "ghp_xxx"
    assert get_secret(slack_token_secret_key("acme")) == "xoxp-yyy"
    assert GITHUB_PAT_SECRET_KEY != slack_token_secret_key("acme")
