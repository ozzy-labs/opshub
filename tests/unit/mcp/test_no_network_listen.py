"""Static guard: the MCP package must not import a network listener
(ADR-0022 §(a) stdio-only).

ADR-0022 forbids HTTP / SSE / Streamable HTTP transport on the MCP
server. The Anthropic Python MCP SDK ships transports for all three
(``mcp.server.stdio`` / ``mcp.server.sse`` /
``mcp.server.streamable_http``), so the negative invariant — *we do
not import the network ones* — is a property of the code, not of the
SDK version pinned in extras. This test enforces it by ``ast``-walking
every ``.py`` file under ``src/opshub/mcp`` and asserting none of them
imports the forbidden modules.

Adding a network transport is a security-relevant change that should
require an ADR amendment (or supersede), so making this test catch
the import at PR time is intentionally noisy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "mcp.server.sse",
        "mcp.server.streamable_http",
        "mcp.server.websocket",
        # Common third-party network listeners; even if a refactor
        # reaches for one of these (e.g. to "just expose health"), the
        # MCP boundary stays stdio-only.
        "uvicorn",
        "fastapi",
        "starlette",
        "aiohttp.web",
        "http.server",
    }
)


def _mcp_files() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[3]
    mcp_dir = repo_root / "src" / "opshub" / "mcp"
    assert mcp_dir.is_dir(), f"expected {mcp_dir} to exist"
    return sorted(p for p in mcp_dir.glob("*.py") if p.is_file())


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


@pytest.mark.parametrize("path", _mcp_files(), ids=lambda p: p.name)
def test_no_forbidden_network_module_imported(path: Path) -> None:
    imports = _imports_in(path)
    offenders = imports & _FORBIDDEN_MODULES
    assert not offenders, (
        f"{path.name} imports forbidden network module(s): {sorted(offenders)} "
        "(ADR-0022 §(a) stdio one transport)"
    )


def test_no_forbidden_module_prefix_imported() -> None:
    """Also catch prefix forms like ``import uvicorn.config``.

    The :func:`_imports_in` helper records dotted names verbatim, so we
    walk every file once more to assert no recorded module starts with
    a forbidden prefix.
    """
    forbidden_prefixes = tuple(m + "." for m in _FORBIDDEN_MODULES)
    for path in _mcp_files():
        for name in _imports_in(path):
            assert not name.startswith(forbidden_prefixes), (
                f"{path.name} imports forbidden network module {name!r}"
            )
