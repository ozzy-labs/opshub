"""Regression guard for opshub#348 — full-suite test isolation of
``opshub.connectors.github.*`` module objects.

Background:
    The ``opshub connector sync`` driver imports every built-in
    connector to populate the registry, so we keep a regression test
    (``test_phase7_slack_sync.test_slack_sync_works_without_github_extra``)
    that simulates the ``connectors-github`` extra being missing by
    blocking ``httpx`` and evicting cached ``opshub.connectors.github*``
    modules from ``sys.modules`` before re-running the CLI.

    The interaction with later tests was subtle (opshub#348): when that
    test ran during ``uv run pytest -q`` (full suite) but **not** when
    only the ``cli/`` folder ran, three later tests in
    ``tests/unit/cli/test_connector_auth.py`` failed —
    ``test_auth_test_github_success``,
    ``test_auth_test_github_failure_exits_1``, and
    ``test_auth_test_renders_empty_values_as_none``. The patch
    ``monkeypatch.setattr(github_auth, "test_token", fake)`` silently
    missed because the polluter (using ``monkeypatch.delitem`` to evict
    ``sys.modules`` entries) left ``sys.modules['…github.auth']`` and
    the parent package's ``opshub.connectors.github.auth`` attribute
    pointing at **different module objects**:

    * ``monkeypatch.delitem`` rolls back by re-inserting the *original*
      module object into ``sys.modules`` on teardown.
    * But the polluter's CLI invoke ran ``import opshub.connectors.github``
      *under the eviction*, which created **new** child module objects
      and rebound the parent package's ``.<submodule>`` attributes to
      those new objects. ``monkeypatch`` does not track parent-attribute
      rebinds — they survive teardown.

    Later, ``import opshub.connectors.github.auth as github_auth``
    resolves via the parent attribute → new module; ``from
    opshub.connectors.github.auth import test_token`` (the CLI's lazy
    import) resolves via ``sys.modules`` → *original* module. The
    monkeypatch.setattr targets the new module; the CLI lookup hits the
    original. Result: the real ``test_token`` runs and tries the
    keyring, which exits 1 in a CI environment without a keyring
    backend.

What this file pins:
    A single-file, order-fixed reproduction so the divergence cannot
    silently re-emerge as the slack / github / ms365 connector tests
    evolve. Pytest collects tests in declared order within a file by
    default, so the ``a``/``b`` naming pins the order without depending
    on ``pytest-ordering`` (which the project does not install).

    * ``test_a_polluter_under_buggy_teardown`` runs the *original*
      ``monkeypatch.delitem`` pattern (the buggy one). Its purpose is
      to leave the in-process state in the divergent shape so the next
      test can demonstrate the divergence is now self-healing.
    * ``test_b_victim_can_patch_test_token_after_polluter`` runs right
      after and verifies that
      ``monkeypatch.setattr(github_auth, "test_token", fake)`` is
      observed by the CLI's lazy ``from opshub.connectors.github.auth
      import test_token``.

    The fix in ``test_phase7_slack_sync.test_slack_sync_works_without_github_extra``
    is *also* applied here (via ``_restore_github_consistently`` in the
    polluter test's ``finally``) so the test passes today. If a future
    refactor reverts to plain ``monkeypatch.delitem`` without the
    consistency restore, this test will fail with ``exit_code == 1`` and
    a stderr containing ``"keyring backend failed to read
    'connector:github:pat'"`` — pointing the reader straight at the bug.
"""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from opshub.cli.app import app

# Trigger an eager import of opshub.connectors.github.auth at module
# load time so the polluter test below operates on a populated
# ``sys.modules`` (otherwise ``monkeypatch.delitem(..., raising=False)``
# is a no-op and the bug shape never materialises in this file). The
# import is also what most real consumers do (``test_connector_auth.py``
# does the equivalent ``from opshub.connectors.github.auth import
# GITHUB_PAT_SECRET_KEY``).
from opshub.connectors.github.auth import (
    GITHUB_PAT_SECRET_KEY,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

# ---------------------------------------------------------------------- helpers


def _ensure_github_registered_after_teardown() -> None:
    """Re-register the github connector so subsequent test modules that
    rely on it (e.g. ``tests/integration/test_phase3_lifecycle.py``)
    find it. The polluter test pops the slot to avoid a registry
    collision when it re-imports ``opshub.connectors.github`` under the
    httpx block; this helper restores the invariant.

    Forces a fresh import by evicting the github sys.modules entries
    first so the ``__init__`` side effect (``register_connector(...)``)
    actually runs (a cached import is a no-op).
    """
    from opshub.connectors._registry import _REGISTRY  # pyright: ignore[reportPrivateUsage]

    if "github" in _REGISTRY:
        return
    for mod_name in list(sys.modules):
        if mod_name == "opshub.connectors.github" or mod_name.startswith(
            "opshub.connectors.github."
        ):
            sys.modules.pop(mod_name, None)
    import opshub.connectors.github  # noqa: F401  (side-effect register)  # pyright: ignore[reportUnusedImport]


def _restore_github_consistently() -> None:
    """Drop every github module and registry slot so the next import
    re-binds ``sys.modules`` and the parent attributes together.

    Mirrors the teardown helper in
    ``test_phase7_slack_sync.test_slack_sync_works_without_github_extra``
    so the two regression sites use the same recovery pattern. If you
    edit one, edit the other (opshub#348).
    """
    from opshub.connectors._registry import _REGISTRY  # pyright: ignore[reportPrivateUsage]

    for mod_name in list(sys.modules):
        if mod_name == "opshub.connectors.github" or mod_name.startswith(
            "opshub.connectors.github."
        ):
            sys.modules.pop(mod_name, None)
    _REGISTRY.pop("github", None)


# ---------------------------------------------------------------------- tests


def test_a_polluter_under_buggy_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the polluter pattern from test_phase7_slack_sync, then
    apply the opshub#348 fix on teardown.

    Two halves:

    1. Use ``monkeypatch.delitem`` exactly the way the original Phase 7
       polluter did — this is the buggy shape that opshub#348 traced.
       The buggy teardown (sys.modules restore but parent-attribute
       drift) is what we want this test to *create* so the next test
       can demonstrate the fix prevents the symptom.
    2. Force a fresh, consistent re-import on teardown via
       ``_restore_github_consistently`` so the divergence is cleaned
       before the next test runs.

    If a future refactor removes step 2 (e.g. "the monkeypatch.delitem
    already does this, the finally is redundant"), ``test_b`` will
    fail with the keyring error and the failure message will point
    straight at this file.
    """
    # Pop the registry slot first so the re-import below can register
    # without a ``ValueError: connector 'github' already registered``
    # collision (the connector's ``__init__.py`` calls
    # ``register_connector(GitHubConnector())`` as an import side
    # effect, and the registry guards against two-instance-same-name).
    from opshub.connectors._registry import _REGISTRY  # pyright: ignore[reportPrivateUsage]

    _REGISTRY.pop("github", None)

    monkeypatch.setitem(sys.modules, "httpx", None)
    for mod_name in list(sys.modules):
        if mod_name == "opshub.connectors.github" or mod_name.startswith(
            "opshub.connectors.github."
        ):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    try:
        # Trigger the re-import of opshub.connectors.github (which
        # rebinds the parent package's .<submodule> attributes to
        # *new* module objects).
        import opshub.connectors.github  # noqa: F401  # pyright: ignore[reportUnusedImport]

        # Sanity: github re-registered under the evict + re-import
        # roundtrip.
        from opshub.connectors import discover_connectors

        assert "github" in {c.name for c in discover_connectors()}
    finally:
        _restore_github_consistently()
        # Restore the registry invariant for downstream tests (e.g.
        # ``tests/integration/test_phase3_lifecycle.py``).
        _ensure_github_registered_after_teardown()


def test_b_victim_can_patch_test_token_after_polluter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the polluter, ``monkeypatch.setattr(github_auth, "test_token", …)``
    must be observed by the CLI's lazy ``from … import test_token``.

    This pins the contract that
    ``tests/unit/cli/test_connector_auth.py::test_auth_test_github_success``
    and friends rely on. If the polluter's teardown regresses (the
    sys.modules / parent-attribute divergence returns), the CLI will
    call the real ``test_token`` (which reads from the keyring) and
    exit with code 1 / a stderr keyring error.
    """
    # Use ``from <pkg> import <name>`` (NOT ``import <pkg>.<name> as
    # ...``). Python 3.13's ``import x.y.z as alias`` rebinds the local
    # name from a freshly-resolved copy on every invocation — even when
    # ``sys.modules['x.y.z']`` holds the cached module — so the patch
    # site (``monkeypatch.setattr(alias, attr, fake)``) and the lookup
    # site (``from x.y.z import attr`` inside the CLI) end up on
    # *different module objects* after a sys.modules-evicting polluter
    # has run. ``from <pkg> import <name>`` resolves through the parent
    # package's attribute and is round-trip stable with the CLI's
    # ``from <pkg>.<name> import <attr>`` lookup. See opshub#348 for
    # the diagnosis.
    from opshub.connectors.github import auth as github_auth

    # Pre-condition: the module object obtained via ``from <pkg> import
    # <name>`` resolves the parent attribute, which the polluter's
    # teardown must restore consistently with the ``sys.modules`` entry.
    assert github_auth is sys.modules["opshub.connectors.github.auth"], (
        "github.auth module object diverged between sys.modules and the "
        "parent package attribute — the polluter test left sys.modules "
        "and the parent .auth attribute pointing at different objects. "
        "See opshub#348."
    )

    def fake_test_token() -> dict[str, str]:
        return {"login": "alice", "name": "Alice", "scopes": "repo"}

    monkeypatch.setattr(github_auth, "test_token", fake_test_token)

    runner = CliRunner()
    result = runner.invoke(app, ["connector", "auth", "test", "github"])

    assert result.exit_code == 0, (
        f"auth test github failed after a polluter test ran first.\n"
        f"  stdout: {result.stdout!r}\n"
        f"  stderr: {result.stderr!r}\n"
        "If stderr mentions a keyring error, the fake test_token was "
        "not in effect — see opshub#348 for the divergence pattern."
    )
    assert "alice" in result.stdout
    assert "status:    ok" in result.stdout
