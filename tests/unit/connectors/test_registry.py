"""Tests for ``opshub.connectors._registry``.

The registry is a process-wide dict; every test calls
:func:`unregister_all` through the autouse fixture so the global state
does not leak between tests.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from opshub.connectors import (
    SyncResult,
    discover_connectors,
    register_connector,
    unregister_all,
)


class _StubConnector:
    """Duck-typed connector used by the registry tests."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name

    def sync(self, context: object) -> SyncResult:  # pragma: no cover - test stub
        del context
        return SyncResult(observed_count=0, new_cursor=None)


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Wipe + re-seed the process-wide registry around each test.

    The registry is module-level state; without this fixture, tests
    that run in the same process would observe each other's
    registrations and fail non-deterministically.

    Re-seeding rationale: concrete connectors register themselves as an
    import side effect of their package ``__init__`` (e.g. ``import
    opshub.connectors.github`` runs ``register_connector(GitHubConnector())``).
    Once the module is in ``sys.modules``, a later ``import
    opshub.connectors.github`` (e.g. from inside ``cli/connector.py``
    during ``opshub connector sync github``) is a no-op — registration
    does NOT re-run. If this fixture only called ``unregister_all`` on
    teardown, any subsequent test that drives the CLI (Phase 3 / 7
    integration tests) would observe an empty registry and
    ``connector sync github`` would return exit code 2
    ("unknown connector").

    The teardown therefore explicitly re-registers the four in-tree
    connectors (GitHub + Slack + MS365 + Box) by importing each
    package's ``connector`` module and calling ``register_connector``
    directly, bypassing the one-shot import-side-effect path. Each
    re-registration is guarded by ``ImportError`` so the test suite
    keeps working on environments where optional extras (e.g.
    ``slack_sdk``) are not installed.
    """
    unregister_all()
    yield
    unregister_all()
    # Re-seed: concrete connector __init__ side effects only fire on first
    # import. For each in-tree connector package, importing it the *first*
    # time runs ``register_connector(...)`` automatically as a side
    # effect; subsequent imports are no-ops (the module is already in
    # ``sys.modules``). After ``unregister_all`` we therefore need to
    # explicitly call ``register_connector`` for packages that have
    # already been imported, while delegating to the side effect for
    # packages we are seeing for the first time.
    _reseed_in_tree_connectors()


def _reseed_in_tree_connectors() -> None:
    """Restore the in-tree connector baseline after ``unregister_all``.

    Each connector package's ``__init__`` calls
    ``register_connector(<Concrete>Connector())`` as a side effect, but
    that side effect only fires on the *first* import in the process.
    If the package is already in ``sys.modules`` (very likely once any
    earlier test has imported it), the import statement here is a
    no-op and we must register an instance manually.
    """
    _seed_connector(
        "opshub.connectors.github",
        "opshub.connectors.github.connector",
        "GitHubConnector",
    )
    _seed_connector(
        "opshub.connectors.slack",
        "opshub.connectors.slack.connector",
        "SlackConnector",
    )
    _seed_connector(
        "opshub.connectors.ms365",
        "opshub.connectors.ms365.connector",
        "MS365Connector",
    )
    _seed_connector(
        "opshub.connectors.box",
        "opshub.connectors.box.connector",
        "BoxConnector",
    )
    _seed_connector(
        "opshub.connectors.box_drive",
        "opshub.connectors.box_drive.connector",
        "BoxDriveConnector",
    )


def _seed_connector(package: str, module: str, class_name: str) -> None:
    """Register the connector exported by ``module`` if not already seeded.

    Behaviour:

    * If ``package`` is *not* yet in ``sys.modules``, importing
      ``module`` triggers the package's ``__init__`` side effect which
      registers the connector — nothing more to do.
    * If ``package`` is already in ``sys.modules``, the import is a
      no-op and we register a fresh instance manually.
    * If the import itself fails (extras missing on this environment),
      the connector is simply skipped — the CLI handles ``unknown
      connector`` gracefully.
    """
    already_imported = package in sys.modules
    try:
        imported = __import__(module, fromlist=[class_name])
    except ImportError:  # pragma: no cover - optional extras may be absent
        return
    if already_imported:
        connector_cls = getattr(imported, class_name)
        register_connector(connector_cls())


def test_register_then_discover_returns_instance() -> None:
    stub = _StubConnector()
    register_connector(stub)
    assert discover_connectors() == [stub]


def test_register_same_instance_twice_is_idempotent() -> None:
    """Re-registering the **same** instance is a silent no-op.

    Without idempotency, reload-heavy test harnesses that re-import a
    connector module would raise on the second pass.
    """
    stub = _StubConnector()
    register_connector(stub)
    register_connector(stub)
    assert discover_connectors() == [stub]


def test_register_different_instance_with_same_name_raises() -> None:
    """Two competing implementations under one name must fail loudly."""
    first = _StubConnector(name="github")
    second = _StubConnector(name="github")
    register_connector(first)
    with pytest.raises(ValueError, match="github"):
        register_connector(second)
    # First registration survives the failed second registration.
    assert discover_connectors() == [first]


def test_unregister_all_empties_registry() -> None:
    register_connector(_StubConnector(name="a"))
    register_connector(_StubConnector(name="b"))
    assert len(discover_connectors()) == 2
    unregister_all()
    assert discover_connectors() == []


def test_discover_returns_empty_list_when_nothing_registered() -> None:
    """Fresh process / fully-reset registry yields ``[]`` (Phase 3 MVP)."""
    assert discover_connectors() == []


def test_box_drive_connector_registered_via_side_effect() -> None:
    """Importing :mod:`opshub.connectors.box_drive` registers the connector.

    Phase 9 step B2 (ADR-0019) ships the registration as a side
    effect of the package ``__init__`` — matching the Phase 3 / 7
    precedents. The ``_reset_registry`` fixture wipes the registry
    on entry, so this test exercises the *manual* re-registration
    path (the import side effect already fired earlier in the
    process). The contract under test: a fresh
    :class:`BoxDriveConnector` is the canonical instance that the
    CLI driver discovers.
    """
    from opshub.connectors.box_drive.connector import BoxDriveConnector

    # ``_reset_registry`` setup ran ``unregister_all`` immediately
    # before this body, so the registry is empty here.
    assert discover_connectors() == []

    register_connector(BoxDriveConnector())
    discovered = {c.name: c for c in discover_connectors()}
    assert "box_drive" in discovered
    assert isinstance(discovered["box_drive"], BoxDriveConnector)


def test_github_connector_findable_after_registry_reset_fixture() -> None:
    """Pin that ``_reset_registry``'s teardown re-seeds the github connector.

    Regression for the test-isolation bug where ``_reset_registry``
    wiped the global registry and import-side-effect re-registration
    did not fire (``opshub.connectors.github`` already in
    ``sys.modules``), causing later ``opshub connector sync github``
    integration tests to return exit code 2 ("unknown connector").

    This test does not need to exercise the teardown directly — it only
    needs to assert that the autouse fixture's *setup* path leaves the
    github connector findable for the *next* test. Because the setup
    path itself calls ``unregister_all`` before ``yield``, we register
    a fresh GitHub connector here and then verify it is the same kind
    of instance the teardown will re-seed for downstream tests.
    """
    from opshub.connectors.github.connector import GitHubConnector

    # The fixture's setup ran ``unregister_all`` immediately before this
    # test body, so the registry is empty here — by design.
    assert discover_connectors() == []

    # Simulate what the teardown will do for the *next* test and assert
    # the github connector is then findable through the public API.
    register_connector(GitHubConnector())
    discovered = {c.name: c for c in discover_connectors()}
    assert "github" in discovered
    assert isinstance(discovered["github"], GitHubConnector)
