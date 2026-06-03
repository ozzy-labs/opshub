"""Tests for ``opshub google_calendar`` (Phase 17-B, ADR-0031).

Mirrors :mod:`tests.unit.cli.test_google_mail_app`: Calendar (Phase 14)
shares OAuth with google_workspace, so the CLI exposes ``sync`` only.
"""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app


def test_google_calendar_help_mentions_shared_auth() -> None:
    """``opshub google_calendar --help`` explains auth is shared with google_workspace."""
    runner = CliRunner()
    result = runner.invoke(app, ["google_calendar", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "google_workspace" in result.stdout
    assert "auth" in result.stdout
    assert "sync" in result.stdout


def test_google_calendar_auth_set_unavailable() -> None:
    """``opshub google_calendar auth set`` falls through to Typer's exit-2 path."""
    runner = CliRunner()
    result = runner.invoke(app, ["google_calendar", "auth", "set"])
    assert result.exit_code == 2


def test_google_calendar_sync_is_a_registered_command() -> None:
    """``opshub google_calendar sync --help`` resolves cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["google_calendar", "sync", "--help"])
    assert result.exit_code == 0, result.stdout
