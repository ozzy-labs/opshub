"""Tests for ``opshub slack`` help + dispatch (Phase 17-B, ADR-0031)."""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app


def test_slack_help_lists_sync_auth_conversations() -> None:
    """``opshub slack --help`` lists ``sync``, ``auth``, ``conversations``."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "sync" in result.stdout
    assert "auth" in result.stdout
    assert "conversations" in result.stdout


def test_slack_auth_help_lists_set_and_test() -> None:
    """``opshub slack auth --help`` lists ``set`` and ``test``."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "auth", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "set" in result.stdout
    assert "test" in result.stdout


def test_slack_unknown_subcommand_exits_2() -> None:
    """Unknown subcommand under ``slack`` → exit 2."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "nonexistent-verb"])
    assert result.exit_code == 2


def test_slack_sync_help_resolves_cleanly() -> None:
    """``opshub slack sync --help`` resolves cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["slack", "sync", "--help"])
    assert result.exit_code == 0, result.stdout
