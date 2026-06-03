"""Tests for :mod:`opshub.connectors._discovery`.

The discovery helper (`import_connector_modules`) is the SSOT that
both the CLI surface (``opshub connectors`` / ``opshub <X> sync``)
and the MCP ``connector.sync`` handler call before reading the
process-wide registry. It is therefore the single point that
determines which connectors are reachable end-to-end on a fresh
process.

The existing pins were spread across two test modules:

* ``tests/unit/mcp/test_writes.py`` — static source grep for
  ``teams`` / ``onedrive_drive`` / ``google_calendar`` /
  ``google_mail`` (4 of the 10 in-tree connectors).
* ``tests/unit/cli/test_connectors_command.py`` — an E2E
  ``python -m opshub connectors`` subprocess pin that checks
  ``slack`` / ``github`` / ``google_mail`` appear in stdout.

The combination caught the Phase 17-B + Phase 14 drift the helper
was extracted to address, but the helper itself was never tested
in isolation — every regression check went through MCP or CLI as
a proxy. This module fills the gap with three direct pins:

1. **Static source pin (all 10)** — guarantees every in-tree
   connector has an ``import opshub.connectors.<name>`` line in
   the helper's source. Catches "added a connector but forgot to
   update `_discovery.py`" before the next CLI / MCP regression
   surfaces.
2. **Behavioural populate pin (subprocess)** — runs the helper in
   a fresh Python process (``sys.modules`` empty) and asserts every
   connector name lands in the registry. Anchors the actual
   end-to-end side-effect chain that production relies on.
3. **ImportError robustness pin (subprocess)** — simulates a
   partial-extras install by poisoning one connector's entry in
   ``sys.modules`` with ``None`` (the Python idiom for "this module
   is not importable") and asserts the remaining nine still
   register. Pins the ``try / except ImportError`` arms each entry
   wraps.

Subprocess isolation matters because the connector packages
register themselves with the registry as a one-shot import side
effect (``register_connector(...)`` at module top). Once a package
is in the in-process ``sys.modules``, a later ``import
opshub.connectors.<X>`` is a no-op — the registration does **not**
re-run. Running the assertions in a subprocess guarantees the
``register_connector`` calls fire, which is the actual contract
under test.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap

import pytest

# Every in-tree connector that ``import_connector_modules`` must
# reach. Defined here (not imported from the helper) so a future
# refactor that re-derives the list from a tuple inside the helper
# itself does not silently make this pin a tautology.
_EXPECTED_CONNECTORS: tuple[str, ...] = (
    "github",
    "slack",
    "ms365",
    "box",
    "box_drive",
    "onedrive_drive",
    "teams",
    "google_workspace",
    "google_calendar",
    "google_mail",
)


def test_helper_imports_every_in_tree_connector_in_source() -> None:
    """Static pin: every in-tree connector has an ``import`` line in the helper.

    Complements the per-connector grep pins in
    ``tests/unit/mcp/test_writes.py`` (which only cover 4 of the 10
    connectors). A new connector that lands without updating
    ``_discovery.py`` fails this pin loudly with the missing name —
    the operator-facing symptom (``unknown connector`` from MCP, or
    a missing line in ``opshub connectors``) would otherwise wait
    for the next end-to-end smoke run.
    """
    from opshub.connectors import _discovery

    source = inspect.getsource(_discovery)
    for name in _EXPECTED_CONNECTORS:
        dotted = f"opshub.connectors.{name}"
        assert f"import {dotted}" in source, (
            f"import_connector_modules must side-effect-import {dotted!r} "
            f"so the {name} connector registers with the global registry "
            f"before discovery. A new connector added to "
            f"src/opshub/connectors/{name}/ must also be added to "
            f"src/opshub/connectors/_discovery.py."
        )


def test_helper_populates_registry_in_fresh_subprocess() -> None:
    """Behaviour pin: a fresh process sees every connector after one call.

    The in-process registry cannot be tested this way directly —
    once a connector module is in ``sys.modules``, re-importing it
    is a no-op and ``register_connector`` does not re-run. We
    therefore fork a subprocess that has never imported the
    connector packages, call the helper, and read back every
    registered name. This is the closest behavioural mirror of
    what an operator's ``opshub connectors`` invocation hits.
    """
    script = textwrap.dedent(
        """
        from opshub.connectors import discover_connectors
        from opshub.connectors._discovery import import_connector_modules

        import_connector_modules()
        for connector in discover_connectors():
            print(connector.name)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    # ``check=False`` + explicit assertion (over ``check=True``) so a
    # subprocess-side failure (SyntaxError in the inline script,
    # import-time crash in a connector module, ...) surfaces with
    # stdout + stderr in the pytest output instead of being hidden
    # behind a bare ``CalledProcessError``. The sibling test in
    # ``tests/unit/connectors/test_import_clean.py`` uses the same
    # pattern for the same reason.
    assert result.returncode == 0, (
        f"subprocess exited non-zero ({result.returncode}).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    registered = set(result.stdout.strip().splitlines())
    missing = set(_EXPECTED_CONNECTORS) - registered
    assert not missing, (
        f"Expected every in-tree connector to register after one call to "
        f"import_connector_modules; missing: {sorted(missing)}. "
        f"stdout was: {result.stdout!r}"
    )


def test_helper_continues_after_individual_import_error() -> None:
    """Robustness pin: ``ImportError`` on one connector does not block the rest.

    Each ``import opshub.connectors.<X>`` in the helper is wrapped
    in a ``try / except ImportError: pass`` arm so a partial-extras
    install (e.g. only ``[connectors-github]``) can still discover
    GitHub even when other connectors' optional SDKs are absent. A
    refactor that drops the try/except guards on one connector
    would silently break this property — until the next operator
    runs ``opshub connectors`` on a partial install and discovers
    the whole registry is empty.

    We simulate the partial install by writing ``None`` into
    ``sys.modules['opshub.connectors.slack']`` *before* the helper
    runs (the Python idiom: ``None`` in ``sys.modules`` causes the
    next ``import`` to raise ``ImportError``). The other nine
    connectors must still register. The simulated failure runs in
    a subprocess so the poisoned ``sys.modules`` entry cannot leak
    back into the test process.
    """
    poisoned = "opshub.connectors.slack"
    script = textwrap.dedent(
        f"""
        import sys

        # Mark Slack as unimportable. Python re-raises this as
        # ImportError on the next ``import opshub.connectors.slack``.
        sys.modules[{poisoned!r}] = None

        from opshub.connectors import discover_connectors
        from opshub.connectors._discovery import import_connector_modules

        import_connector_modules()
        for connector in discover_connectors():
            print(connector.name)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    # See sibling test for the rationale behind ``check=False`` +
    # explicit assertion (surface stderr to pytest output on failure).
    assert result.returncode == 0, (
        f"subprocess exited non-zero ({result.returncode}).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    registered = set(result.stdout.strip().splitlines())

    assert "slack" not in registered, (
        "Poisoning sys.modules['opshub.connectors.slack'] should have "
        "made the slack import raise ImportError, leaving it out of the "
        f"registry. Got registered={sorted(registered)}."
    )
    others = {name for name in _EXPECTED_CONNECTORS if name != "slack"}
    missing = others - registered
    assert not missing, (
        f"A single connector ImportError must not block the others. "
        f"Expected at least {sorted(others)} to register; "
        f"missing: {sorted(missing)}. stdout={result.stdout!r}"
    )


@pytest.mark.parametrize("name", _EXPECTED_CONNECTORS)
def test_helper_wraps_each_connector_import_in_try_except_importerror(
    name: str,
) -> None:
    """Shape pin: every connector's import line is guarded by ``try / except``.

    Pre-PR-#437 the inline MCP block in ``mcp/_writes.py`` had per-line
    drift — Gmail in particular carried an extra ``# noqa: F401`` while
    the other connectors did not, signalling unequal care across
    entries. The post-extraction helper should treat every connector
    consistently: each import sits inside its own ``try / except
    ImportError: pass`` block.

    The pin asserts the ``try:`` + ``except ImportError:`` shape is
    present near each import line. This is a coarse grep — false
    positives are possible if the helper grows multiple imports per
    try block — but for the current shape (one connector per
    ``try``) it cheaply catches a future refactor that drops the
    guard on a single entry.
    """
    from opshub.connectors import _discovery

    source = inspect.getsource(_discovery)
    dotted = f"opshub.connectors.{name}"

    # Find the line with the import, then walk back to the nearest
    # ``try:`` and forward to the nearest ``except ImportError``.
    lines = source.splitlines()
    import_idx: int | None = None
    for i, line in enumerate(lines):
        if f"import {dotted}" in line:
            import_idx = i
            break
    assert import_idx is not None, f"no import line for {dotted}"

    # Look back up to 3 lines for the ``try:`` opener.
    preceding = "\n".join(lines[max(0, import_idx - 3) : import_idx])
    assert "try:" in preceding, (
        f"{dotted} import must be wrapped in a ``try:`` block so "
        f"partial-extras installs do not break discovery of the other "
        f"connectors."
    )

    # Look forward up to 3 lines for the ``except ImportError`` handler.
    following = "\n".join(lines[import_idx : min(len(lines), import_idx + 4)])
    assert "except ImportError" in following, (
        f"{dotted} import must be followed by an ``except ImportError`` "
        f"handler so a missing optional extra does not abort the rest "
        f"of discovery."
    )
