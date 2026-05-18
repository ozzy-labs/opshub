"""Bootstrap smoke tests."""

from __future__ import annotations

from typer.testing import CliRunner

from opshub import __version__
from opshub.cli.app import app


def test_version_constant_is_set() -> None:
    assert __version__ == "0.1.1"


def test_cli_version_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
