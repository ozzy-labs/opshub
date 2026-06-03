"""Tests for ``opshub connectors`` (Phase 17-B, ADR-0031).

Replaces the legacy ``opshub connector list`` command. Output is
byte-identical to the legacy surface (so any operator scripts grepping
the list keep working).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app
from opshub.connectors import SyncResult, register_connector, unregister_all


class _StubConnector:
    """Minimal connector used to populate the registry under test."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name

    def sync(self, context: object) -> SyncResult:  # pragma: no cover - not invoked
        del context
        return SyncResult(observed_count=0, new_cursor=None)


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Isolate every test from the process-wide connector registry."""
    unregister_all()
    yield
    unregister_all()


def test_connectors_empty_registry_prints_friendly_message() -> None:
    """Empty registry → friendly hint, exit 0."""
    runner = CliRunner()
    result = runner.invoke(app, ["connectors"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == "no connectors registered"


def test_connectors_after_register_prints_names() -> None:
    """Registered connectors appear in output, one per line."""
    register_connector(_StubConnector(name="stub-a"))
    register_connector(_StubConnector(name="stub-b"))

    runner = CliRunner()
    result = runner.invoke(app, ["connectors"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().splitlines()
    assert "stub-a" in lines
    assert "stub-b" in lines


def test_connectors_includes_box_drive_after_import() -> None:
    """Importing :mod:`opshub.connectors.box_drive` adds it to ``connectors`` output."""
    import importlib

    import opshub.connectors.box_drive

    importlib.reload(opshub.connectors.box_drive)

    runner = CliRunner()
    result = runner.invoke(app, ["connectors"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().splitlines()
    assert "box_drive" in lines


def test_legacy_connector_group_no_longer_exists() -> None:
    """``opshub connector`` (singular) is fully removed (BREAKING CHANGE).

    ADR-0031 §決定 (7) confirms no backward-compat alias is shipped:
    operators must migrate to the new per-noun groups. Pinning the
    absence here means a future contributor cannot accidentally
    re-introduce the legacy surface via a stray ``add_typer``.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["connector"])
    # Typer reports unknown subcommands with exit code 2 (usage error).
    assert result.exit_code == 2
    # The error message names the unknown command.
    assert "connector" in (result.stdout + result.stderr).lower()
