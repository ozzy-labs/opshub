"""Tests for ``opshub google_mail`` (Phase 17-B, ADR-0031).

Gmail (Phase 14) shares OAuth with google_workspace, so the CLI
exposes ``sync`` only — auth lives under ``opshub google_workspace
auth set``. The ``--help`` text must surface this so operators
know where to provision credentials.
"""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app


def test_google_mail_help_mentions_shared_auth() -> None:
    """``opshub google_mail --help`` explains auth is shared with google_workspace."""
    runner = CliRunner()
    result = runner.invoke(app, ["google_mail", "--help"])

    assert result.exit_code == 0, result.stdout
    # The help text must point operators at ``opshub google_workspace
    # auth set`` so they know where to provision credentials.
    assert "google_workspace" in result.stdout
    assert "auth" in result.stdout
    assert "sync" in result.stdout


def test_google_mail_auth_set_unavailable() -> None:
    """``opshub google_mail auth set`` falls through to Typer's exit-2 path."""
    runner = CliRunner()
    result = runner.invoke(app, ["google_mail", "auth", "set"])
    assert result.exit_code == 2


def test_google_mail_sync_is_a_registered_command() -> None:
    """``opshub google_mail sync --help`` resolves cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["google_mail", "sync", "--help"])
    assert result.exit_code == 0, result.stdout
