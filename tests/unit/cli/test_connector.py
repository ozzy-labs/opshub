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


def test_connector_list_includes_box_drive_after_import() -> None:
    """Importing :mod:`opshub.connectors.box_drive` adds it to ``list`` output.

    Phase 9 step B2 (ADR-0019) ships the box_drive connector with
    side-effect registration. The autouse ``_reset_registry``
    fixture wipes the registry, so we re-fire the side effect via
    :func:`importlib.reload` and verify ``opshub connector list``
    surfaces ``box_drive`` to the operator. This is the
    cross-cutting check that prevents a future refactor from
    silently dropping the registry call.
    """
    import importlib

    import opshub.connectors.box_drive

    importlib.reload(opshub.connectors.box_drive)

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "list"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().splitlines()
    assert "box_drive" in lines


def test_connector_auth_set_rejects_box_drive() -> None:
    """``auth set connector:box_drive`` exits 2 with an actionable hint.

    ADR-0019 §決定 (a)(g): the box_drive connector reads a local
    Box Drive mount point and has no token / OAuth surface. The CLI
    must fail-fast with a pointer to ``opshub.toml`` configuration
    rather than silently storing an unused token.
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["connector", "auth", "set", "connector:box_drive", "--token", "ignored"],
    )
    assert result.exit_code == 2
    # The error message names the configuration key and the ADR so
    # operators can self-correct without grepping the codebase.
    assert "root_path" in result.stderr
    assert "[connectors.box_drive]" in result.stderr
    assert "0019" in result.stderr
