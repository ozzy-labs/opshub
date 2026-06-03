"""Tests for ``opshub box_drive`` (Phase 17-B, ADR-0031 §決定 (6)).

The box_drive connector has no auth surface — operators configure
``root_path`` in ``opshub.toml`` and the OS Box Drive client handles
authentication. The CLI exposes ``sync`` only; ``auth set`` falls
through Typer's default "No such command" exit-2 path.
"""

from __future__ import annotations

from typer.testing import CliRunner

from opshub.cli.app import app


def test_box_drive_help_lists_sync_only() -> None:
    """``opshub box_drive --help`` shows ``sync`` and no ``auth`` group.

    We assert no ``auth`` command is registered by exercising the
    dispatch path (``opshub box_drive auth set`` → exit 2 ``no such
    command``); the help text itself mentions ``auth`` in the
    explanatory blurb ("No auth surface — configure root_path...")
    so a substring check on stdout would be too coarse.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["box_drive", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "sync" in result.stdout


def test_box_drive_auth_set_exits_2_with_no_such_command() -> None:
    """``opshub box_drive auth set`` falls through to Typer's exit-2 ``No such command``.

    ADR-0031 §決定 (6) prefers ``command unavailable`` (Typer's
    ``Usage:`` exit-2 path) over a no-op reject. Operators see an
    explicit "this is not a thing" rather than "this exists but does
    nothing".
    """
    runner = CliRunner()
    result = runner.invoke(app, ["box_drive", "auth", "set"])

    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).lower()
    # Typer's natural "No such command" message contains the unknown
    # command name; the exact wording is Typer's, not ours, so we
    # assert on the key tokens rather than the full string.
    assert "no such command" in combined or "usage" in combined
    assert "auth" in combined


def test_box_drive_auth_test_also_unavailable() -> None:
    """``opshub box_drive auth test`` is equally unavailable."""
    runner = CliRunner()
    result = runner.invoke(app, ["box_drive", "auth", "test"])
    assert result.exit_code == 2


def test_box_drive_sync_is_a_registered_command() -> None:
    """``opshub box_drive sync --help`` resolves cleanly (does not exit 2)."""
    runner = CliRunner()
    result = runner.invoke(app, ["box_drive", "sync", "--help"])
    assert result.exit_code == 0, result.stdout
