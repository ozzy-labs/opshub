"""Tests for ``opshub google_workspace`` help + dispatch (Phase 17-B, ADR-0031)."""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app


def test_google_workspace_help_lists_sync_and_auth() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "sync" in result.stdout
    assert "auth" in result.stdout


def test_google_workspace_auth_help_lists_set_and_test() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "auth", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "set" in result.stdout
    assert "test" in result.stdout


def test_google_workspace_help_mentions_shared_auth_scope() -> None:
    """``--help`` text mentions that auth is shared with Gmail + Calendar."""
    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "--help"])
    assert result.exit_code == 0
    # The shared-scope context is important context for operators.
    assert "Gmail" in result.stdout or "Calendar" in result.stdout


def test_google_workspace_unknown_subcommand_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["google_workspace", "bogus"])
    assert result.exit_code == 2
