"""Real-Chromium integration test for the ``browser.fetch`` MCP tool (Phase 21-D).

Behind the ``browser`` marker (ADR-0037 §決定 (g) / epic #504 test
plan), this drives the **full MCP dispatch path** — not the browser core
directly. It calls :func:`opshub.mcp.server.dispatch_tool_call` for the
real ``browser.fetch`` spec (built by
:func:`opshub.mcp.server.build_tool_specs_for_engine`), so the test
exercises the registry policy + the async→sync ``asyncio.to_thread``
bridge + the snippet truncation + the redaction wrapper end-to-end with
a live headless Chromium.

The page is served by a **localhost** ``http.server`` so the test never
reaches the public network (symmetric with
``tests/integration/test_browser_core.py``). The embedded script mutates
the DOM after load, proving the rendered-DOM value of the browser layer.

Skipped automatically when ``playwright`` is not installed or the
Chromium binary has not been provisioned, so a plain
``uv run pytest -m browser`` on a dev machine that has not run
``playwright install chromium`` stays green. CI provisions both via the
``playwright install chromium --with-deps`` step.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")
pytest.importorskip(
    "mcp",
    reason="browser.fetch MCP dispatch test requires the 'mcp' extra",
)

from opshub.db.engine import create_engine_for_sqlite
from opshub.mcp.server import build_tool_specs_for_engine, dispatch_tool_call

pytestmark = pytest.mark.browser


_HTML_PAGE = """\
<!doctype html>
<html>
  <head><title>opshub mcp fetch page</title></head>
  <body>
    <h1>Heading</h1>
    <p id="static">static body text</p>
    <p id="dynamic"></p>
    <script>
      document.getElementById('dynamic').textContent = 'js injected text';
    </script>
  </body>
</html>
"""


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # http.server overrides this API name
        body = _HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Silence the default stderr access logging during the test run.
        pass


@pytest.fixture
def local_server() -> Iterator[str]:
    # Bind to loopback only so the fixture never serves the public network.
    server = HTTPServer(("127.0.0.1", 0), _PageHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_browser_fetch_dispatch_returns_rendered_text(
    local_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dispatch_tool_call("browser.fetch", ...)`` renders a localhost page.

    Drives the real MCP dispatch path so the registry spec, the
    ``asyncio.to_thread`` bridge, the snippet truncation, and the
    TextContent wrapping are all exercised with a live Chromium. The
    JS-injected text only exists in the *rendered* DOM, so its presence
    in the returned snippet is the Playwright-adoption value (ADR-0037
    §決定 (d)) surfacing all the way through the MCP boundary.
    """
    # The handler resolves ``OpsHubSettings()`` itself; point the
    # dedicated browser user-data-dir at the tmp_path via env so the
    # test never touches the operator's real data dir.
    monkeypatch.setenv("OPSHUB_DATA_DIR", str(tmp_path / "data"))

    engine = create_engine_for_sqlite(tmp_path / "mcp.sqlite")
    try:
        specs = build_tool_specs_for_engine(engine)
        specs_by_name = {s.name: s for s in specs}
        assert "browser.fetch" in specs_by_name

        try:
            content = await dispatch_tool_call(
                specs_by_name, "browser.fetch", {"url": local_server}
            )
        except Exception as exc:  # narrow to env-not-ready skip below
            message = str(exc)
            if (
                "playwright install" in message
                or "Chromium is not installed" in message
                or message.startswith("failed to launch browser")
            ):
                pytest.skip(f"Chromium not provisioned: {message}")
            raise
    finally:
        engine.dispose()

    assert len(content) == 1
    payload: dict[str, Any] = json.loads(content[0].text)
    assert payload["ok"] is True
    assert payload["title"] == "opshub mcp fetch page"
    assert "static body text" in payload["text"]
    # Rendered-DOM proof: JS-injected text surfaces through the MCP path.
    assert "js injected text" in payload["text"]
    # Ad-hoc read: nothing persisted to the event log / projection.
    assert payload["persisted"] is False
