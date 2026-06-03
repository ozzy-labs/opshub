"""MCP ``connector.sync`` widening pins.

Pins that the MCP ``connector.sync`` write handler can reach every
in-tree connector — originally from Phase 11 audit Cluster B (H4),
which caught the CLI / MCP drift for Teams + OneDrive Drive
(``connector.sync`` raised ``OpsHubError("unknown connector ...")``
for both names even though the CLI was wired). Phase 14 added
``google_calendar`` / ``google_mail`` and the MCP inline import block
silently missed them too — same shape of drift. Subsequently the
import set was consolidated into
:mod:`opshub.connectors._discovery` (``import_connector_modules``),
so the static pins below now grep that SSOT instead of the MCP
handler module. Either way the contract is unchanged: the MCP path
must register the same connector set the CLI does, before
dispatching.

The tests assert two things:

1. **Static source check** — the discovery helper's source contains
   an ``import opshub.connectors.<name>`` line for every connector
   the MCP handler dispatches to. Cheap, deterministic, does not
   depend on registry state.
2. **Dynamic registry check** — after the handler runs, the registry
   exposes the connector name end-to-end. We mock the registered
   connector instance so we don't exercise the real SaaS sync path;
   the contract under test is "the handler reaches the connector via
   discovery", not the connector's own behaviour.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, cast

import pytest

from opshub.connectors.base import SyncResult

# ---------------------------------------------------------------------------
# Static source check — the discovery helper must mention every connector the
# MCP handler dispatches to.
#
# Pre-extraction these pins grepped ``opshub.mcp._writes`` directly because
# the import block was duplicated inline in the handler. The handler now
# delegates to ``opshub.connectors._discovery.import_connector_modules``,
# so the SSOT moved with it. The grep target is correspondingly re-anchored
# on the discovery module — the contract under test is unchanged.
# ---------------------------------------------------------------------------


def test_discovery_helper_imports_teams_connector() -> None:
    """The discovery helper must import ``opshub.connectors.teams``.

    Phase 11 audit Cluster B (H4) recorded the original CLI / MCP
    drift for Teams. A regression that drops the line would silently
    break MCP ``connector.sync`` for Teams again. We grep the module
    source rather than executing it because the import runs lazily
    inside the helper and re-running it across tests would couple the
    assertion to ``sys.modules`` state.
    """
    from opshub.connectors import _discovery

    source = inspect.getsource(_discovery)
    assert "import opshub.connectors.teams" in source, (
        "MCP connector.sync handler must reach Teams via "
        "import_connector_modules — the helper must side-effect-import "
        "opshub.connectors.teams so the Teams connector registers with "
        "the global registry before discovery."
    )


def test_discovery_helper_imports_onedrive_drive_connector() -> None:
    """The discovery helper must import ``opshub.connectors.onedrive_drive``.

    Symmetric to the Teams pin — OneDrive Drive is the second
    Phase 11 connector the MCP write handler historically missed.
    """
    from opshub.connectors import _discovery

    source = inspect.getsource(_discovery)
    assert "import opshub.connectors.onedrive_drive" in source, (
        "MCP connector.sync handler must reach OneDrive Drive via "
        "import_connector_modules — the helper must side-effect-import "
        "opshub.connectors.onedrive_drive so the OneDrive Drive connector "
        "registers with the global registry before discovery."
    )


def test_discovery_helper_imports_google_calendar_connector() -> None:
    """The discovery helper must import ``opshub.connectors.google_calendar``.

    Phase 14 added the Google Calendar connector. The MCP inline
    import block (pre-extraction) missed it, so MCP clients invoking
    ``connector.sync`` with ``name="google_calendar"`` received
    ``OpsHubError("unknown connector ...")`` even though the CLI was
    wired. The extraction to ``_discovery`` closed that gap; this pin
    keeps it shut.
    """
    from opshub.connectors import _discovery

    source = inspect.getsource(_discovery)
    assert "import opshub.connectors.google_calendar" in source, (
        "MCP connector.sync handler must reach Google Calendar via "
        "import_connector_modules — the helper must side-effect-import "
        "opshub.connectors.google_calendar so the connector registers "
        "with the global registry before discovery."
    )


def test_discovery_helper_imports_google_mail_connector() -> None:
    """The discovery helper must import ``opshub.connectors.google_mail``.

    Symmetric to the Google Calendar pin — Phase 14's Gmail
    connector was the second connector the MCP inline import block
    silently missed.
    """
    from opshub.connectors import _discovery

    source = inspect.getsource(_discovery)
    assert "import opshub.connectors.google_mail" in source, (
        "MCP connector.sync handler must reach Gmail via "
        "import_connector_modules — the helper must side-effect-import "
        "opshub.connectors.google_mail so the connector registers with "
        "the global registry before discovery."
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
async def test_connector_sync_handler_dispatches_google_calendar_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connector.sync`` with ``name="google_calendar"`` reaches ``.sync()``.

    Phase 14 added Google Calendar but the MCP write handler's
    inline import block was not updated (the same shape of CLI / MCP
    drift Phase 11 closed for Teams + OneDrive Drive). PR #437
    closed the gap by routing the MCP handler through
    :func:`opshub.connectors._discovery.import_connector_modules`;
    this test is the behavioural counterpart to the static source
    pin (``test_discovery_helper_imports_google_calendar_connector``)
    — it proves the name resolves through the handler end-to-end,
    not just that the import line exists in source.
    """
    from opshub.mcp._writes import build_connector_sync_handler

    stub_service = _StubSourceService()
    _install_stub_source_service(monkeypatch, stub_service)
    stub_connector = _install_stub_connector(monkeypatch, "google_calendar")

    handler = build_connector_sync_handler(engine=cast("Any", None))
    raw = await handler({"name": "google_calendar"})

    assert stub_connector.sync_called is True
    payload = cast("dict[str, Any]", json.loads(raw))
    assert payload["ok"] is True
    assert payload["connector"] == "google_calendar"
    assert payload["observed_count"] == 0


@pytest.mark.asyncio
async def test_connector_sync_handler_dispatches_google_mail_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connector.sync`` with ``name="google_mail"`` reaches ``.sync()``.

    Symmetric to the Google Calendar test — Gmail was the second
    Phase 14 connector the pre-PR-#437 inline MCP import block
    silently missed. The static source pin
    (``test_discovery_helper_imports_google_mail_connector``) shows
    the import line exists in ``_discovery``; this test shows the
    handler actually dispatches when the name comes in.
    """
    from opshub.mcp._writes import build_connector_sync_handler

    stub_service = _StubSourceService()
    _install_stub_source_service(monkeypatch, stub_service)
    stub_connector = _install_stub_connector(monkeypatch, "google_mail")

    handler = build_connector_sync_handler(engine=cast("Any", None))
    raw = await handler({"name": "google_mail"})

    assert stub_connector.sync_called is True
    payload = cast("dict[str, Any]", json.loads(raw))
    assert payload["ok"] is True
    assert payload["connector"] == "google_mail"
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
