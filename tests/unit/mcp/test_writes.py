"""Phase 11 audit Cluster B (H4) — MCP ``connector.sync`` widening.

Pins that the MCP ``connector.sync`` write handler imports the full
Phase 11 connector set (github + slack + ms365 + box + box_drive +
**teams + onedrive_drive**) before dispatching. The H4 audit finding
recorded the CLI imported both Phase 11 connectors but the MCP write
handler did not, so an MCP client invoking ``connector.sync`` with
``name="teams"`` or ``name="onedrive_drive"`` received an
``OpsHubError("unknown connector ...")`` even when the connector was
fully wired on the CLI side.

The tests assert two things:

1. **Static source check** — the handler module's source contains the
   ``import opshub.connectors.teams`` and
   ``import opshub.connectors.onedrive_drive`` lines. This is the
   cheap, deterministic check that does not depend on registry state.
2. **Dynamic registry check** — after the handler's side-effect
   imports run, the registry exposes both connector names. We mock
   the registered connector instance so we don't exercise the real
   SaaS sync path; the contract under test is "the handler reaches
   the connector via discovery", not the connector's own behaviour.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, cast

import pytest

from opshub.connectors.base import SyncResult

# ---------------------------------------------------------------------------
# Static source check — the import block must mention both new connectors
# ---------------------------------------------------------------------------


def test_writes_module_imports_teams_connector() -> None:
    """The handler module's source must import ``opshub.connectors.teams``.

    The H4 fix added the side-effect import; a regression that drops
    the line would silently break MCP ``connector.sync`` for Teams
    again. We grep the module source rather than executing it because
    the import runs inside an async handler and re-running it across
    tests would couple the assertion to ``sys.modules`` state.
    """
    from opshub.mcp import _writes

    source = inspect.getsource(_writes)
    assert "import opshub.connectors.teams" in source, (
        "Phase 11 audit Cluster B (H4): MCP connector.sync handler must "
        "side-effect-import opshub.connectors.teams so the Teams connector "
        "registers with the global registry before discovery."
    )


def test_writes_module_imports_onedrive_drive_connector() -> None:
    """The handler module's source must import ``opshub.connectors.onedrive_drive``.

    Symmetric to the teams check — OneDrive Drive is the second
    Phase 11 connector the MCP write handler historically missed.
    """
    from opshub.mcp import _writes

    source = inspect.getsource(_writes)
    assert "import opshub.connectors.onedrive_drive" in source, (
        "Phase 11 audit Cluster B (H4): MCP connector.sync handler must "
        "side-effect-import opshub.connectors.onedrive_drive so the "
        "OneDrive Drive connector registers with the global registry "
        "before discovery."
    )


# ---------------------------------------------------------------------------
# Dynamic registry check — the handler accepts both names end-to-end
# ---------------------------------------------------------------------------


class _StubSourceService:
    """Minimal :class:`SourceService` substitute for the handler.

    The handler reads ``cursor_get`` / ``cursor_set`` before dispatch
    and may call ``record_sync_failure`` on the error path. We stub
    each to a deterministic no-op so the handler exercises the
    connector-discovery branch (the actual contract under test)
    without touching the real engine wiring.
    """

    def __init__(self) -> None:
        self.cursors: dict[str, str | None] = {}
        self.failures: list[tuple[str, str]] = []

    def cursor_get(self, name: str) -> str | None:
        return self.cursors.get(name)

    def cursor_set(self, name: str, value: str | None, *, sync_started: bool) -> None:
        _ = sync_started
        self.cursors[name] = value

    def record_sync_failure(self, name: str, *, error_message: str) -> None:
        self.failures.append((name, error_message))


def _install_stub_source_service(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubSourceService,
) -> None:
    """Replace ``build_source_service`` with a factory returning ``stub``."""

    def _factory(actor: str) -> _StubSourceService:
        _ = actor
        return stub

    monkeypatch.setattr("opshub.cli._wiring.build_source_service", _factory)


class _StubConnector:
    """Connector stub that records the sync call without touching SaaS."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sync_called = False

    def sync(self, context: object) -> SyncResult:
        _ = context
        self.sync_called = True
        return SyncResult(observed_count=0, new_cursor="cursor-stub")


def _install_stub_connector(monkeypatch: pytest.MonkeyPatch, name: str) -> _StubConnector:
    """Force the connector named ``name`` to resolve to a stub.

    The handler's :func:`discover_connectors` call returns whatever the
    process-wide registry holds at dispatch time. The fixture re-seed
    in ``tests/unit/connectors/test_registry.py`` re-registers Teams /
    OneDrive Drive instances at module level, but a per-test connector
    swap is cleaner: we override :func:`discover_connectors` itself so
    only the connector under test is visible. This avoids touching the
    private ``_REGISTRY`` dict (silencing pyright) and decouples the
    test from import-order surprises.
    """
    stub = _StubConnector(name)
    monkeypatch.setattr(
        "opshub.connectors.discover_connectors",
        lambda: [stub],
    )
    return stub


@pytest.mark.asyncio
async def test_connector_sync_handler_dispatches_teams_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connector.sync`` with ``name="teams"`` reaches the connector's ``.sync()``.

    Pre-fix the handler raised ``OpsHubError("unknown connector
    'teams'")`` because the side-effect import was missing. Post-fix
    the discovery returns the registered Teams instance and the
    handler dispatches to ``.sync()``. We swap ``discover_connectors``
    for a factory that returns a stub so the assertion runs even
    without the ``[connectors-teams]`` extras installed (the
    side-effect import inside the handler may fail with ImportError,
    which the handler swallows; the override here is what makes the
    name discoverable in any environment).
    """
    from opshub.mcp._writes import build_connector_sync_handler

    stub_service = _StubSourceService()
    _install_stub_source_service(monkeypatch, stub_service)
    stub_connector = _install_stub_connector(monkeypatch, "teams")

    handler = build_connector_sync_handler(engine=cast("Any", None))
    raw = await handler({"name": "teams"})

    assert stub_connector.sync_called is True
    payload = cast("dict[str, Any]", json.loads(raw))
    assert payload["ok"] is True
    assert payload["connector"] == "teams"
    assert payload["observed_count"] == 0


@pytest.mark.asyncio
async def test_connector_sync_handler_dispatches_onedrive_drive_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connector.sync`` with ``name="onedrive_drive"`` reaches ``.sync()``.

    Symmetric to the Teams test. Pre-fix: ``OpsHubError("unknown
    connector 'onedrive_drive'")``. Post-fix: discovery yields the
    registered OneDriveDriveConnector and dispatch succeeds. The
    OneDrive Drive connector does **not** require optional extras
    (stat-only walk by default), so this test runs unconditionally.
    """
    from opshub.mcp._writes import build_connector_sync_handler

    stub_service = _StubSourceService()
    _install_stub_source_service(monkeypatch, stub_service)
    stub_connector = _install_stub_connector(monkeypatch, "onedrive_drive")

    handler = build_connector_sync_handler(engine=cast("Any", None))
    raw = await handler({"name": "onedrive_drive"})

    assert stub_connector.sync_called is True
    payload = cast("dict[str, Any]", json.loads(raw))
    assert payload["ok"] is True
    assert payload["connector"] == "onedrive_drive"
    assert payload["observed_count"] == 0


@pytest.mark.asyncio
async def test_connector_sync_handler_unknown_name_raises_opshub_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown connector names still raise ``OpsHubError`` after the H4 fix.

    Backwards-compatibility guard: the H4 fix added entries to the
    side-effect import block but must not relax the unknown-name
    error path. We pin the original behaviour for any name not in the
    discovery set.
    """
    from opshub.connectors.base import Connector
    from opshub.core.errors import OpsHubError
    from opshub.mcp._writes import build_connector_sync_handler

    stub_service = _StubSourceService()
    _install_stub_source_service(monkeypatch, stub_service)

    # Empty discovery — any name should be unknown. Annotate the
    # lambda's return so pyright can resolve the parameterised
    # ``list[Connector]`` type (without it, the lambda's empty list
    # surfaces as ``list[Unknown]``).
    def _empty_discovery() -> list[Connector]:
        return []

    monkeypatch.setattr("opshub.connectors.discover_connectors", _empty_discovery)

    handler = build_connector_sync_handler(engine=cast("Any", None))

    with pytest.raises(OpsHubError) as excinfo:
        await handler({"name": "bogus_connector_xyz"})

    assert "bogus_connector_xyz" in str(excinfo.value)


def test_writes_call_handler_uses_correct_argument_name() -> None:
    """Argument shape sanity check — the handler keys on ``name``.

    Pinned indirectly: changing the argument key from ``name`` to
    anything else would break every MCP client that calls
    ``connector.sync`` with the documented schema (see
    :mod:`opshub.mcp._registry` ``connector.sync`` ``inputSchema``).
    The handler reads ``arguments["name"]`` so a typo would raise a
    :class:`KeyError`. A regression here is cheap to catch with a
    quick :func:`inspect.getsource` grep, parallel to the side-effect
    import sentinel tests above.
    """
    from opshub.mcp import _writes

    source = inspect.getsource(_writes.build_connector_sync_handler)
    assert 'arguments["name"]' in source, (
        "connector.sync handler must read the 'name' argument from the "
        "MCP arguments mapping. See opshub.mcp._registry for the schema."
    )
