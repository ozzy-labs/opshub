"""Every built-in connector package must import without its extra installed.

Regression guard for #198. ``opshub connector sync <name>`` imports
*all* built-in connectors to populate the process-wide registry (each
``opshub.connectors.<name>.__init__`` registers its connector as an
import side effect). Therefore importing any one connector package must
**not** eagerly pull a heavy SDK that belongs to *another* connector's
extra — otherwise an operator who installed only e.g.
``[connectors-slack]`` hits ``ModuleNotFoundError`` when syncing Slack,
because the CLI driver also imports the github package.

This is the behavioural counterpart to ``phase-7-plan`` §"Cold-start
guard / import-clean contract" (connector packages keep heavy SDK
imports inside method bodies). It is enforced by *import*, not by AST
scanning, on purpose: a connector's SDK-wrapping submodule (e.g.
``github/api.py``) legitimately imports ``httpx`` at its own module
level — the invariant is that the *package* import chain never reaches
it, because ``connector.py`` imports it lazily inside ``sync``.

Each check runs in a **fresh subprocess** with every connector SDK
blocked (``sys.modules[name] = None`` makes ``import name`` raise
``ImportError``). A subprocess is used rather than in-process
``importlib`` gymnastics so the test cannot pollute the parent
interpreter (re-executing connector modules leaves global drift that
``monkeypatch`` cannot fully undo) and so the simulation is a faithful
"fresh interpreter, extra not installed" environment.

It catches the exact class of regression that broke Slack sync:
``github/connector.py`` used to ``import api`` (→ ``httpx``) at module
level, so importing the github package required the
``connectors-github`` extra.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Heavy third-party SDKs shipped in the connector extras. Blocking all of
# them at once proves a package import path relies on *none* of them.
# ``github`` is PyGithub's top-level name (distinct from the
# ``opshub.connectors.github`` package).
_HEAVY_SDKS: tuple[str, ...] = (
    "httpx",
    "msal",
    "boxsdk",
    "slack_sdk",
    "github",
    "msgraph",
)

# (package, registered connector name) for every built-in connector.
_CONNECTORS: list[tuple[str, str]] = [
    ("opshub.connectors.github", "github"),
    ("opshub.connectors.slack", "slack"),
    ("opshub.connectors.ms365", "ms365"),
    ("opshub.connectors.box", "box"),
    ("opshub.connectors.box_drive", "box_drive"),
]


def _import_clean_script(package: str, connector_name: str) -> str:
    """Build the subprocess body that imports ``package`` with SDKs blocked.

    Blocks every connector SDK *before* importing opshub, imports the
    package (running its registration side effect), and asserts the
    connector landed in the registry. Exits non-zero with a diagnostic on
    failure so the parent surfaces it in the assertion message.
    """
    return textwrap.dedent(
        f"""
        import sys

        # Simulate "no connector extras installed": a None entry makes a
        # subsequent ``import <sdk>`` raise ImportError.
        for _sdk in {_HEAVY_SDKS!r}:
            sys.modules[_sdk] = None

        import {package}  # noqa: F401  (registration side effect)

        from opshub.connectors import discover_connectors

        registered = {{c.name for c in discover_connectors()}}
        if {connector_name!r} not in registered:
            sys.stderr.write(
                "connector {connector_name!r} not registered after importing "
                "{package!r}; registered=" + repr(sorted(registered))
            )
            raise SystemExit(1)
        """
    )


@pytest.mark.parametrize(
    ("package", "connector_name"),
    _CONNECTORS,
    ids=[name for _, name in _CONNECTORS],
)
def test_connector_package_imports_without_any_extra(
    package: str,
    connector_name: str,
) -> None:
    """Importing ``package`` registers its connector even with every SDK absent.

    A non-zero subprocess exit means a heavy SDK is being imported on the
    package import path — the fix is to defer that import into a method
    body (see :mod:`opshub.connectors.github` for the pattern).
    """
    proc = subprocess.run(
        [sys.executable, "-c", _import_clean_script(package, connector_name)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"importing {package!r} with all connector SDKs blocked failed "
        f"(exit {proc.returncode}). A heavy SDK is on the package import "
        f"path; defer it into a method body.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
