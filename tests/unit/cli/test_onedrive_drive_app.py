"""Tests for ``opshub onedrive_drive`` (Phase 17-B, ADR-0031 §決定 (6)).

Mirrors :mod:`tests.unit.cli.test_box_drive_app`: onedrive_drive has
no auth surface, only ``sync``.
"""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app


def test_onedrive_drive_help_lists_sync_only() -> None:
    """``opshub onedrive_drive --help`` shows ``sync`` and no ``auth`` group.

    See ``test_box_drive_app`` for the rationale: dispatch-path
    assertion is more reliable than a stdout substring check because
    the help blurb mentions ``auth`` in prose.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["onedrive_drive", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "sync" in result.stdout


def test_onedrive_drive_auth_set_exits_2() -> None:
    """``opshub onedrive_drive auth set`` falls through to Typer's exit-2 path."""
    runner = CliRunner()
    result = runner.invoke(app, ["onedrive_drive", "auth", "set"])

    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).lower()
    assert "no such command" in combined or "usage" in combined


def test_onedrive_drive_auth_test_exits_2() -> None:
    """``opshub onedrive_drive auth test`` is equally unavailable."""
    runner = CliRunner()
    result = runner.invoke(app, ["onedrive_drive", "auth", "test"])
    assert result.exit_code == 2


def test_onedrive_drive_sync_is_a_registered_command() -> None:
    """``opshub onedrive_drive sync --help`` resolves cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["onedrive_drive", "sync", "--help"])
    assert result.exit_code == 0, result.stdout
