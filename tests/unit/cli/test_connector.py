"""Tests for ``opshub connector list`` / ``opshub connector sync``.

Phase 3 step A5 ships the CLI placeholder. No concrete connector is
registered yet (sub-issue B adds GitHub), so the tests focus on:

* Empty registry → ``list`` prints ``no connectors registered`` (exit 0).
* Populated registry → ``list`` prints each connector name (exit 0).
* Unknown name → ``sync`` prints a helpful message on stderr and exits 2.

The ``sync`` happy path is **not** exercised here because it requires a
wired :class:`SourceService` (step A4) and at least one concrete
connector (sub-issue B). That coverage lands in the GitHub connector
integration test (PR B3).
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


def test_connector_list_empty_registry_prints_friendly_message() -> None:
    """Phase 3 MVP state: no connectors registered → friendly hint, exit 0."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "list"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == "no connectors registered"


def test_connector_list_after_register_prints_names() -> None:
    """Registered connectors appear in ``list`` output, one per line."""
    register_connector(_StubConnector(name="stub-a"))
    register_connector(_StubConnector(name="stub-b"))

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "list"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().splitlines()
    assert "stub-a" in lines
    assert "stub-b" in lines


def test_connector_sync_unknown_name_exits_2_with_helpful_stderr() -> None:
    """Asking ``sync`` for a name the registry does not know is a usage error.

    Typer's convention is exit code 2 for usage errors; the message
    lists available connectors so the user can self-correct.
    """
    register_connector(_StubConnector(name="stub"))

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "sync", "nope"])
    assert result.exit_code == 2
    # Helpful diagnostic on stderr — the unknown name plus what is available.
    assert "nope" in result.stderr
    assert "stub" in result.stderr


def test_connector_sync_unknown_name_with_empty_registry_reports_none() -> None:
    """With no connectors at all, the diagnostic still parses cleanly."""
    runner = CliRunner()
    result = runner.invoke(app, ["connector", "sync", "ghost"])
    assert result.exit_code == 2
    assert "ghost" in result.stderr
    # "(none)" appears verbatim when the registry is empty.
    assert "(none)" in result.stderr
