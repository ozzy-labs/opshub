"""Tests for ``opshub teams auth set / test`` (Phase 17-B, ADR-0031)."""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors.teams.auth import TEAMS_TOKEN_SECRET_KEY
from opshub.core.secrets import get_secret
from tests.unit.cli.conftest import InMemoryKeyring


def test_teams_auth_set_with_token_flag_stores_to_keyring(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``--token`` writes to ``connector:teams:token``."""
    runner = CliRunner()
    result = runner.invoke(app, ["teams", "auth", "set", "--token", "teams-test"])

    assert result.exit_code == 0, result.stdout
    assert "teams" in result.stdout
    assert get_secret(TEAMS_TOKEN_SECRET_KEY) == "teams-test"


def test_teams_auth_set_with_stdin_prompt(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """No ``--token`` → prompt; stdin feeds the hidden prompt."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["teams", "auth", "set"],
        input="teams-prompted\n",
    )

    assert result.exit_code == 0, result.stdout
    assert get_secret(TEAMS_TOKEN_SECRET_KEY) == "teams-prompted"


def test_teams_auth_set_rejects_empty_token(
    in_memory_keyring: InMemoryKeyring,
) -> None:
    """``--token ""`` is rejected."""
    runner = CliRunner()
    result = runner.invoke(app, ["teams", "auth", "set", "--token", ""])

    assert result.exit_code == 2
    assert "non-empty" in result.stderr
    assert get_secret(TEAMS_TOKEN_SECRET_KEY) is None


def test_teams_auth_test_surfaces_unsupported_message() -> None:
    """``teams auth test`` is a friendly stub pending a real verifier.

    The legacy ``opshub connector auth test`` dispatch had no teams
    arm either (exit 2 unknown). The new per-noun stub surfaces a
    ``ConfigError`` pointing operators at ``opshub teams sync`` for
    end-to-end verification — exit 1 with ``status: failed``.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["teams", "auth", "test"])

    assert result.exit_code == 1
    assert "status:    failed" in result.stderr
    assert "opshub teams sync" in result.stderr
