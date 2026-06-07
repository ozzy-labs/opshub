"""Real-Chromium smoke test for :mod:`opshub.browser.core` (Phase 21-B).

Behind the ``browser`` marker (ADR-0037 §決定 (g) / epic #504 test
plan). It launches a real headless Chromium (provisioned by the CI
``playwright install chromium`` step) and fetches a page served by a
**localhost** ``http.server`` — so the test never reaches the public
network.

The page embeds a tiny script that mutates the DOM after load, proving
the value of using a real browser: a static-HTML fetch would miss the
JS-injected text, but ``page.inner_text("body")`` picks it up because we
read the *rendered* DOM (ADR-0037 §決定 (d)).

The test is skipped automatically when ``playwright`` is not installed
(no ``[browser]`` extra) or when the Chromium binary has not been
provisioned (``ConfigError`` from ``fetch_page``) so a plain
``uv run pytest`` on a dev machine without ``playwright install
chromium`` stays green.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from opshub.browser.core import BrowserFetchError, PageContent, fetch_page
from opshub.core.config import OpsHubSettings
from opshub.core.errors import ConfigError

pytest.importorskip("playwright")

pytestmark = pytest.mark.browser


def _fetch_or_skip(url: str, *, settings: OpsHubSettings, max_chars: int = 500_000) -> PageContent:
    """Fetch ``url`` or ``pytest.skip`` when the browser env is not provisioned.

    Two env-not-ready failure shapes are treated as a skip so a plain
    ``uv run pytest -m browser`` on a dev machine that has not run
    ``playwright install chromium --with-deps`` stays green:

    * :class:`ConfigError` — the Chromium binary is absent entirely.
    * :class:`BrowserFetchError` whose message starts with
      ``"failed to launch browser"`` — the binary exists but the host
      is missing the OS-level shared libraries Chromium needs (the
      ``--with-deps`` case). A *navigation* ``BrowserFetchError`` (raised
      only after a successful launch) is **not** skipped, so a real
      regression in the fetch path still fails the test. CI provisions
      both via the ``playwright install chromium --with-deps`` step, so
      the test runs for real there.
    """
    try:
        return fetch_page(url, settings=settings, max_chars=max_chars)
    except ConfigError as exc:
        pytest.skip(f"Chromium binary not provisioned: {exc}")
    except BrowserFetchError as exc:
        if str(exc).startswith("failed to launch browser"):
            pytest.skip(f"Chromium cannot launch (missing OS deps?): {exc}")
        raise


_HTML_PAGE = """\
<!doctype html>
<html>
  <head><title>opshub smoke page</title></head>
  <body>
    <h1>Static heading</h1>
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
        # ``format`` shadows the builtin to match the base-class API name.
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


@pytest.fixture
def settings(tmp_path: Path) -> OpsHubSettings:
    return OpsHubSettings(data_dir=tmp_path / "data")


def test_fetch_renders_and_extracts_js_injected_text(
    local_server: str,
    settings: OpsHubSettings,
) -> None:
    result = _fetch_or_skip(local_server, settings=settings)

    assert result.title == "opshub smoke page"
    assert "static body text" in result.text
    # The JS-injected text is only present in the *rendered* DOM —
    # this is the Playwright-adoption value (ADR-0037 §決定 (a)/(d)).
    assert "js injected text" in result.text
    assert result.truncated is False


def test_fetch_caps_large_page_body(
    tmp_path: Path,
    settings: OpsHubSettings,
) -> None:
    # Serve a very large static file so the 500K char cap trips against a
    # real render. Use the explicit ``max_chars`` override to keep the
    # fixture small (a 100-char body capped at 10).
    big_dir = tmp_path / "site"
    big_dir.mkdir()
    (big_dir / "index.html").write_text(
        "<!doctype html><html><head><title>big</title></head>"
        "<body><p>" + ("z" * 100) + "</p></body></html>",
        encoding="utf-8",
    )

    handler = partial(SimpleHTTPRequestHandler, directory=str(big_dir))
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/index.html"
        result = _fetch_or_skip(url, settings=settings, max_chars=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.truncated is True
    assert result.text.startswith("z" * 10)
    assert "browser body truncated" in result.text
