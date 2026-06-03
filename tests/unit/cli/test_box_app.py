"""Tests for ``opshub box`` help + dispatch (Phase 17-B, ADR-0031)."""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app


def test_box_help_lists_sync_and_auth() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["box", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "sync" in result.stdout
    assert "auth" in result.stdout


def test_box_auth_help_lists_set_and_test() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["box", "auth", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "set" in result.stdout
    assert "test" in result.stdout


def test_box_unknown_subcommand_exits_2() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["box", "bogus"])
    assert result.exit_code == 2
