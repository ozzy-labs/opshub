"""Tests for ``opshub slack auth set / test`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors.slack.auth import slack_token_secret_key
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
        ["slack", "auth", "set", "--workspace", "acme", "--token", "xoxp-test"],
    )

    assert result.exit_code == 0, result.stdout
    assert "slack" in result.stdout
    assert get_secret(slack_token_secret_key("acme")) == "xoxp-test"


def test_slack_auth_set_emits_next_action_hint(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Phase 23-E (#535): ``auth set`` points at ``auth test`` on stderr."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["slack", "auth", "set", "--workspace", "acme", "--token", "xoxp-test"]
    )

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
        ["slack", "auth", "set", "--workspace", "acme"],
        input="xoxp-prompted\n",
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(slack_token_secret_key("acme")) == "xoxp-prompted"


def test_slack_auth_set_strips_surrounding_whitespace(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Pasted token with trailing whitespace is stored clean."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["slack", "auth", "set", "--workspace", "acme", "--token", "  xoxp-test  "],
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(slack_token_secret_key("acme")) == "xoxp-test"


# ----- error paths ---------------------------------------------------------


def test_slack_auth_set_rejects_empty_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``--token ""`` is a user error, not a "stored empty token"."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "set", "--workspace", "acme", "--token", ""])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(slack_token_secret_key("acme")) is None


def test_slack_auth_set_rejects_whitespace_only_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Whitespace-only tokens are also rejected."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "set", "--workspace", "acme", "--token", "   "])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(slack_token_secret_key("acme")) is None


# ----- env-var override ----------------------------------------------------


def test_slack_token_env_var_name_per_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 24-C (ADR-0041 §(a)): the env override is per workspace alias.

    ``connector:slack:<alias>:token`` derives
    ``OPSHUB_CONNECTOR_SLACK_<ALIAS>_TOKEN`` via the standard
    ``_env_var_name`` folding; env wins over keyring per ADR-0014. The
    alias grammar bans ``-`` so the folding stays injective.
    """
    monkeypatch.setenv("OPSHUB_CONNECTOR_SLACK_ACME_TOKEN", "xoxp-env-wins")
    assert get_secret(slack_token_secret_key("acme")) == "xoxp-env-wins"


# ----- auth test happy path -----------------------------------------------


def test_slack_auth_test_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """``slack auth test`` delegates to ``SlackAuth.test_token`` and renders the dict."""
    import opshub.connectors.slack.auth as slack_auth

    class _FakeSlackAuth:
        def __init__(self, alias: str) -> None:
            self.alias = alias

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
    result = runner.invoke(app, ["slack", "auth", "test", "--workspace", "acme"])

    assert result.exit_code == 0, result.stdout
    assert "connector: slack" in result.stdout
    assert "principal" in result.stdout
    assert "user" in result.stdout
    # Granted scopes are surfaced so operators can verify a token carries
    # the history scopes ``sync`` needs before hitting ``missing_scope``
    # (#533, byte-symmetric with ``opshub github auth test``).
    assert "scopes" in result.stdout
    assert "channels:history,channels:read,users:read" in result.stdout
    # Phase 23-I (#539, ADR-0040): a features readiness block follows the
    # field list on stdout. With channels:history + users:read the public
    # sync is READY; the private / DM / mpim syncs and engagement are MISSING.
    assert "features:" in result.stdout
    assert "public channel sync: READY" in result.stdout
    assert "DM sync: MISSING im:history" in result.stdout
    assert "engagement axis (--sort=last_self_post): MISSING search:read" in result.stdout
    # The membership caveat is surfaced once so READY is never read as a
    # guarantee that the token can actually read a channel.
    assert "does not guarantee channel membership" in result.stdout
    # Phase 23-E (#535): a successful test points at the discovery step.
    assert "next:" in result.stderr
    assert "opshub slack conversations --format=toml" in result.stderr


def test_slack_auth_test_features_block_bot_principal_marks_engagement_na(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Bot Token reports the engagement axis as ``N/A``, not ``MISSING`` —
    a Bot Token cannot hold ``search:read`` (ADR-0034), so suggesting it as a
    fixable gap would mislead (Phase 23-I, #539)."""
    import opshub.connectors.slack.auth as slack_auth

    class _FakeSlackAuth:
        def __init__(self, alias: str) -> None:
            self.alias = alias

        def test_token(self) -> dict[str, str]:
            return {
                "team": "Acme",
                "team_id": "T1",
                "user": "acme-bot",
                "user_id": "U1",
                "principal": "bot",
                "scopes": "channels:history,users:read",
            }

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    result = CliRunner().invoke(app, ["slack", "auth", "test", "--workspace", "acme"])

    assert result.exit_code == 0, result.stdout
    assert "engagement axis (--sort=last_self_post): N/A (User Token only)" in result.stdout
    assert "MISSING search:read" not in result.stdout


def test_slack_auth_test_features_block_degrades_without_scopes_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Slack omits the scopes header the block cannot assess readiness and
    says so rather than emitting a misleading all-MISSING verdict (#539)."""
    import opshub.connectors.slack.auth as slack_auth

    class _FakeSlackAuth:
        def __init__(self, alias: str) -> None:
            self.alias = alias

        def test_token(self) -> dict[str, str]:
            return {
                "team": "Acme",
                "team_id": "T1",
                "user": "alice",
                "user_id": "U1",
                "principal": "user",
                "scopes": "",
            }

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    result = CliRunner().invoke(app, ["slack", "auth", "test", "--workspace", "acme"])

    assert result.exit_code == 0, result.stdout
    assert "features:" in result.stdout
    assert "cannot assess readiness" in result.stdout


def test_slack_auth_test_failure_does_not_emit_next_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next-action hint is for the *success* path only (#535)."""
    import opshub.connectors.slack.auth as slack_auth
    from opshub.core.errors import ConfigError

    class _FakeSlackAuth:
        def __init__(self, alias: str) -> None:
            self.alias = alias

        def test_token(self) -> dict[str, str]:
            raise ConfigError("Slack auth.test returned non-ok: invalid_auth")

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "test", "--workspace", "acme"])

    assert result.exit_code == 1
    assert "next:" not in result.stderr


def test_slack_auth_test_failure_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ConfigError`` from the connector surfaces as exit code 1 + status: failed."""
    import opshub.connectors.slack.auth as slack_auth
    from opshub.core.errors import ConfigError

    class _FakeSlackAuth:
        def __init__(self, alias: str) -> None:
            self.alias = alias

        def test_token(self) -> dict[str, str]:
            raise ConfigError("Slack auth.test returned non-ok: invalid_auth")

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "test", "--workspace", "acme"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "invalid_auth" in result.stderr


def test_slack_auth_test_renders_empty_values_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-string values render as ``(none)``."""
    import opshub.connectors.slack.auth as slack_auth

    class _FakeSlackAuth:
        def __init__(self, alias: str) -> None:
            self.alias = alias

        def test_token(self) -> dict[str, str]:
            return {"team": "Acme", "user": ""}

    monkeypatch.setattr(slack_auth, "SlackAuth", _FakeSlackAuth)

    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "test", "--workspace", "acme"])

    assert result.exit_code == 0
    assert "(none)" in result.stdout


# ----- no `auth get` -------------------------------------------------------


def test_slack_auth_does_not_expose_get_subcommand() -> None:
    """Security policy: no ``auth get`` (tokens must not echo to stdout)."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "get"])
    assert result.exit_code != 0


# ----- --workspace default resolution (Phase 24-C, ADR-0041 §(f)) ----------


def _patch_settings_workspaces(monkeypatch: pytest.MonkeyPatch, aliases: list[str]) -> None:
    """Patch ``OpsHubSettings`` so the configured workspace set is ``aliases``."""
    import opshub.core.config as opshub_config
    from opshub.core.config import ConnectorSettings, OpsHubSettings, SlackConnectorSettings

    slack = SlackConnectorSettings.model_validate(
        {"workspaces": {alias: {"channels": ["C1"]} for alias in aliases}}
    )
    settings = OpsHubSettings(connectors=ConnectorSettings(slack=slack))
    monkeypatch.setattr(opshub_config, "OpsHubSettings", lambda: settings)


def test_slack_auth_set_defaults_to_single_configured_workspace(
    in_memory_keyring: InMemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One configured workspace → the flag is optional (ADR-0041 §(f))."""
    _patch_settings_workspaces(monkeypatch, ["acme"])
    result = CliRunner().invoke(app, ["slack", "auth", "set", "--token", "xoxp-test"])

    assert result.exit_code == 0, result.stdout
    assert get_secret(slack_token_secret_key("acme")) == "xoxp-test"


def test_slack_auth_set_with_zero_workspaces_requires_flag(
    in_memory_keyring: InMemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings_workspaces(monkeypatch, [])
    result = CliRunner().invoke(app, ["slack", "auth", "set", "--token", "xoxp-test"])

    assert result.exit_code == 1
    assert "no Slack workspaces configured" in result.stderr
    assert "--workspace" in result.stderr


def test_slack_auth_set_with_multiple_workspaces_requires_flag(
    in_memory_keyring: InMemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple workspaces → ambiguity is loud and the aliases are listed."""
    _patch_settings_workspaces(monkeypatch, ["acme", "oss"])
    result = CliRunner().invoke(app, ["slack", "auth", "set", "--token", "xoxp-test"])

    assert result.exit_code == 1
    assert "multiple Slack workspaces configured" in result.stderr
    assert "acme" in result.stderr
    assert "oss" in result.stderr
    assert get_secret(slack_token_secret_key("acme")) is None
    assert get_secret(slack_token_secret_key("oss")) is None


def test_slack_auth_set_rejects_invalid_alias_format(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """A hyphenated alias is rejected before any keyring write (ADR-0041 §(a))."""
    result = CliRunner().invoke(
        app, ["slack", "auth", "set", "--workspace", "my-ws", "--token", "xoxp-test"]
    )

    assert result.exit_code == 1
    assert "invalid Slack workspace alias" in result.stderr
    assert get_secret("connector:slack:my-ws:token") is None


def test_slack_auth_set_writes_per_alias_slots_independently(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """Two aliases store two independent tokens (ADR-0041 §(a))."""
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["slack", "auth", "set", "--workspace", "acme", "--token", "xoxp-a"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["slack", "auth", "set", "--workspace", "oss", "--token", "xoxb-b"]
        ).exit_code
        == 0
    )

    assert get_secret(slack_token_secret_key("acme")) == "xoxp-a"
    assert get_secret(slack_token_secret_key("oss")) == "xoxb-b"
