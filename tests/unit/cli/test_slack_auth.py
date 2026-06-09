"""Tests for ``opshub slack auth set / test`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors.slack.auth import SLACK_TOKEN_SECRET_KEY
from opshub.core.secrets import get_secret
from tests.unit.cli.conftest import InMemoryKeyring

pytest.importorskip(
    "slack_sdk",
    reason="Slack auth tests require the 'connectors-slack' extras",
)


# ----- happy path: token paste --------------------------------------------


def test_slack_auth_set_with_token_flag_stores_to_keyring(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``--token`` writes to the keyring slot the SlackAuth reader uses."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "auth", "set", "--token", "xoxp-test"],
    )

    assert result.exit_code == 0, result.stdout
    assert "slack" in result.stdout
    assert get_secret(SLACK_TOKEN_SECRET_KEY) == "xoxp-test"


def test_slack_auth_set_emits_next_action_hint(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Phase 23-E (#535): ``auth set`` points at ``auth test`` on stderr."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "set", "--token", "xoxp-test"])

    assert result.exit_code == 0, result.stdout
    assert "next:" in result.stderr
    assert "opshub slack auth test" in result.stderr
    # The hint is stderr-only so the stdout success line stays parseable.
    assert "next:" not in result.stdout


def test_slack_auth_set_with_stdin_prompt(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """No ``--token`` → prompt; CliRunner.input feeds the hidden prompt."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "auth", "set"],
        input="xoxp-prompted\n",
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(SLACK_TOKEN_SECRET_KEY) == "xoxp-prompted"


def test_slack_auth_set_strips_surrounding_whitespace(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Pasted token with trailing whitespace is stored clean."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "auth", "set", "--token", "  xoxp-test  "],
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(SLACK_TOKEN_SECRET_KEY) == "xoxp-test"


# ----- error paths ---------------------------------------------------------


def test_slack_auth_set_rejects_empty_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``--token ""`` is a user error, not a "stored empty token"."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "set", "--token", ""])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(SLACK_TOKEN_SECRET_KEY) is None


def test_slack_auth_set_rejects_whitespace_only_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Whitespace-only tokens are also rejected."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "set", "--token", "   "])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(SLACK_TOKEN_SECRET_KEY) is None


# ----- env-var override ----------------------------------------------------


def test_slack_token_env_var_name_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 17-B keeps ``OPSHUB_CONNECTOR_SLACK_TOKEN`` env var unchanged.

    ADR-0031 §non-goals explicitly pins keyring key / env var override
    names as unchanged — only the CLI command surface moves. This test
    pins that contract by exercising the env-var precedence rule
    (env wins over keyring per ADR-0014).
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_TOKEN", "xoxp-env-wins")
    assert get_secret(SLACK_TOKEN_SECRET_KEY) == "xoxp-env-wins"


# ----- auth test happy path -----------------------------------------------


def test_slack_auth_test_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """``slack auth test`` delegates to ``SlackAuth.test_token`` and renders the dict."""
    import opshub.connectors.slack.auth as slack_auth

    class _FakeSlackAuth:
        def __init__(self) -> None:
            pass

        def test_token(self) -> dict[str, str]:
            return {
                "team": "Acme",
                "team_id": "T1",
                "user": "alice",
                "user_id": "U1",
                "principal": "user",
                "scopes": "channels:history,channels:read,users:read",
            }

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "test"])

    assert result.exit_code == 0, result.stdout
    assert "connector: slack" in result.stdout
    assert "principal" in result.stdout
    assert "user" in result.stdout
    # Granted scopes are surfaced so operators can verify a token carries
    # the history scopes ``sync`` needs before hitting ``missing_scope``
    # (#533, byte-symmetric with ``opshub github auth test``).
    assert "scopes" in result.stdout
    assert "channels:history,channels:read,users:read" in result.stdout
    # Phase 23-E (#535): a successful test points at the discovery step.
    assert "next:" in result.stderr
    assert "opshub slack conversations --format=toml" in result.stderr


def test_slack_auth_test_failure_does_not_emit_next_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next-action hint is for the *success* path only (#535)."""
    import opshub.connectors.slack.auth as slack_auth
    from opshub.core.errors import ConfigError

    class _FakeSlackAuth:
        def __init__(self) -> None:
            pass

        def test_token(self) -> dict[str, str]:
            raise ConfigError("Slack auth.test returned non-ok: invalid_auth")

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "test"])

    assert result.exit_code == 1
    assert "next:" not in result.stderr


def test_slack_auth_test_failure_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ConfigError`` from the connector surfaces as exit code 1 + status: failed."""
    import opshub.connectors.slack.auth as slack_auth
    from opshub.core.errors import ConfigError

    class _FakeSlackAuth:
        def __init__(self) -> None:
            pass

        def test_token(self) -> dict[str, str]:
            raise ConfigError("Slack auth.test returned non-ok: invalid_auth")

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "test"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "invalid_auth" in result.stderr


def test_slack_auth_test_renders_empty_values_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-string values render as ``(none)``."""
    import opshub.connectors.slack.auth as slack_auth

    class _FakeSlackAuth:
        def __init__(self) -> None:
            pass

        def test_token(self) -> dict[str, str]:
            return {"team": "Acme", "user": ""}

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "test"])

    assert result.exit_code == 0
    assert "(none)" in result.stdout


# ----- no `auth get` -------------------------------------------------------


def test_slack_auth_does_not_expose_get_subcommand() -> None:
    """Security policy: no ``auth get`` (tokens must not echo to stdout)."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "get"])
    assert result.exit_code != 0
