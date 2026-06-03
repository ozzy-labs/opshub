"""Tests for ``opshub connectors`` (Phase 17-B, ADR-0031).

Replaces the legacy ``opshub connector list`` command. Output is
byte-identical to the legacy surface (so any operator scripts grepping
the list keep working).
"""

from __future__ import annotations

import inspect
import subprocess
import sys
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


def test_connectors_empty_registry_prints_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty registry → friendly hint, exit 0.

    We force-empty the registry via a monkeypatch on
    ``discover_connectors`` rather than relying on
    ``unregister_all()``. Reason: the ``connectors_list`` callback
    now auto-imports every connector subpackage as a side effect (the
    fix for PR #414's "no connectors registered" regression), so on a
    fresh process the import-side-effect ``register_connector(...)``
    repopulates the registry even after the autouse fixture wipes it.
    The behaviour we still want to pin here is the *message branch*:
    when discovery returns nothing the command prints the friendly
    hint and exits 0 — orthogonal to whether connectors happen to be
    wired in-tree.
    """
    from opshub.connectors.base import Connector

    def _empty() -> list[Connector]:
        return []

    monkeypatch.setattr("opshub.connectors.discover_connectors", _empty)

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
    """Importing :mod:`opshub.connectors.box_drive` adds it to ``connectors`` output.

    Pins the import-side-effect contract (``register_connector`` runs
    at module top). This test alone does NOT catch the Phase 17-B
    regression where the ``connectors_list`` callback forgot to call
    the discovery helper, because the test manually pre-imports
    ``box_drive`` before invoking the CLI — the registry is therefore
    pre-populated and the missing helper call is invisible. The
    ``test_connectors_list_callback_invokes_discovery_helper`` and
    ``test_connectors_in_fresh_subprocess_lists_real_connectors``
    tests below cover that gap.
    """
    import importlib

    import opshub.connectors.box_drive

    importlib.reload(opshub.connectors.box_drive)

    runner = CliRunner()
    result = runner.invoke(app, ["connectors"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().splitlines()
    assert "box_drive" in lines


def test_connectors_list_callback_invokes_discovery_helper() -> None:
    """The ``connectors_list`` callback must call ``import_connector_modules``.

    Regression pin for the Phase 17-B (PR #414) bug: the refactor
    split ``opshub connector list`` into the new ``opshub connectors``
    surface but forgot to carry over the connector-module import
    call. On a fresh Python process the registry then started empty
    and the command misleadingly reported ``no connectors
    registered`` even though every connector was wired in-tree.

    We pin the contract via static source inspection because the
    autouse ``_reset_registry`` fixture wipes the registry, and on
    re-invocation the connector modules are already in ``sys.modules``
    — so the side-effect ``register_connector(...)`` would not re-run
    even if the helper is called. The structural pin sidesteps that
    ``sys.modules`` trap; the subprocess pin below exercises the
    end-to-end behaviour in a fresh process.
    """
    from opshub.cli import connectors as cli_connectors

    source = inspect.getsource(cli_connectors)
    assert "import_connector_modules" in source, (
        "opshub connectors regression (PR #414 fix): the connectors_list "
        "callback must call import_connector_modules before "
        "discover_connectors so the registry is populated on a fresh "
        "process. Without this call the command reports 'no connectors "
        "registered' even though every connector is wired in-tree."
    )


def test_connectors_in_fresh_subprocess_lists_real_connectors() -> None:
    """End-to-end pin: ``opshub connectors`` in a fresh process lists real connectors.

    This mirrors what an operator sees when they run the command at
    a terminal — the autouse registry-wipe fixture does not apply
    here because the subprocess has its own Python interpreter (so
    real connector modules import-side-effect-register cleanly). If
    the connectors_list callback ever loses its import call again,
    this test fails the same way the operator's invocation would.

    Kept distinct from the in-process structural pin above so a
    regression catches both the structural defect (no helper call)
    and the behavioural defect (output is empty) — either one is
    enough to bisect, but together they make the failure mode
    obvious.
    """
    result = subprocess.run(
        [sys.executable, "-m", "opshub", "connectors"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.strip().splitlines()
    # ``slack`` and ``github`` are mandatory in-tree connectors;
    # ``google_mail`` is the Phase 14 connector the MCP inline import
    # block historically missed — pinning it here keeps both the
    # Phase 17-B CLI bug and the Phase 14 MCP gap nailed down.
    assert "slack" in lines, result.stdout
    assert "github" in lines, result.stdout
    assert "google_mail" in lines, result.stdout


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
