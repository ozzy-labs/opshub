"""Tests for ``opshub web`` (Phase 21-C, ADR-0037 / ADR-0031 §決定 (6)).

The web connector has no auth surface — a public Web page needs no token.
The CLI exposes ``sync`` only; ``auth set`` falls through Typer's default
"No such command" exit-2 path (the box_drive precedent).
"""

from __future__ import annotations

import subprocess
import sys

from typer.testing import CliRunner

from opshub.cli.app import app


def test_web_help_lists_sync_only() -> None:
    """``opshub web --help`` shows ``sync`` and resolves cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["web", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "sync" in result.stdout


def test_web_auth_set_exits_2_with_no_such_command() -> None:
    """``opshub web auth set`` falls through to Typer's exit-2 path.

    ADR-0031 §決定 (6) prefers ``command unavailable`` (Typer's ``Usage:``
    exit-2 path) over a no-op reject.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["web", "auth", "set"])

    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).lower()
    assert "no such command" in combined or "usage" in combined
    assert "auth" in combined


def test_web_sync_is_a_registered_command() -> None:
    """``opshub web sync --help`` resolves cleanly (does not exit 2)."""
    runner = CliRunner()
    result = runner.invoke(app, ["web", "sync", "--help"])
    assert result.exit_code == 0, result.stdout


def test_web_appears_in_connectors_list() -> None:
    """``opshub connectors`` lists the registered ``web`` connector.

    Runs ``python -m opshub connectors`` as a **subprocess** so the
    process-wide connector registry is freshly populated by each
    subpackage's import-side-effect ``register_connector(...)``. An
    in-process :class:`~typer.testing.CliRunner` invocation would be
    fragile: a prior test in the session may have called
    ``unregister_all()``, and the ``connectors`` callback's
    ``import_connector_modules()`` is a no-op once the modules are cached
    in ``sys.modules`` (the same reasoning ``test_connectors_command.py``
    documents), so the registry could be empty. A fresh subprocess sidesteps
    that shared-state hazard entirely.
    """
    result = subprocess.run(
        [sys.executable, "-m", "opshub", "connectors"],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "web" in result.stdout.split()
