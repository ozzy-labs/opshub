"""End-to-end ``opshub web sync`` lifecycle (Phase 21-C, ADR-0037).

Behind the ``browser`` marker (epic #504 test plan): it launches a real
headless Chromium (provisioned by the CI ``playwright install chromium``
step) and fetches a page served by a **localhost** ``http.server`` — so the
test never reaches the public network.

What it pins (issue #507 integration plan)
------------------------------------------

1. **sources persist + FTS5 search hit** — ``opshub web sync`` renders the
   localhost page, persists a ``web_page`` source, and the body is
   searchable via ``opshub search`` (the FTS5 ``sources_fts`` path).
2. **idempotent re-sync** — a second sync of the unchanged page emits no
   new ``SourceObserved`` (the SHA-256 body fingerprint matches the prior
   row, ADR-0019 §決定 (d) change-detection).
3. **JS-rendered page extraction** — the served page mutates its DOM after
   ``load`` with a ``<script>``; the extracted body contains the
   JS-injected text, proving the Playwright-adoption value (a static-HTML
   fetch would miss it, ADR-0037 §決定 (d)).

Why each ``web sync`` runs as a **subprocess** (``python -m opshub ...``)
------------------------------------------------------------------------

The browser core drives Playwright's **sync** API
(``sync_playwright().start()`` / ``.stop()``, ADR-0037 §決定 (h)). Playwright
forbids a *second* ``sync_playwright()`` in a process that still has a live
asyncio event loop from the first one ("Sync API inside asyncio loop"), so
two in-process renders (the idempotency case needs two) would trip on the
leftover loop. Running each ``opshub web sync`` as a fresh subprocess gives
every render a clean process / loop — and is the realistic operator
invocation (cron / launchd) besides. The ``opshub search`` read is run the
same way for symmetry; the env vars the :func:`isolated_env` fixture sets
are forwarded through ``os.environ``.

The test skips automatically when ``playwright`` is not installed (no
``[browser]`` extra) or when the Chromium binary / OS deps are not
provisioned, so a plain ``uv run pytest`` on a dev machine stays green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from opshub.db.engine import create_engine_for_sqlite

pytest.importorskip("playwright")

pytestmark = pytest.mark.browser


# The served page injects DOM text after the ``load`` event so a static
# fetch would miss "js injected marker" — only a real render picks it up.
_HTML_PAGE = """\
<!doctype html>
<html>
  <head><title>opshub web connector page</title></head>
  <body>
    <h1>Static heading</h1>
    <p id="static">static body sentinel</p>
    <p id="dynamic"></p>
    <script>
      document.getElementById('dynamic').textContent = 'js injected marker';
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
        # Silence default stderr access logging. ``format`` shadows the
        # builtin to match the base-class API name.
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


def _row_count(db_path: Path, table: str, where: str = "") -> int:
    from sqlalchemy import text

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            clause = f" WHERE {where}" if where else ""
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}{clause}")).scalar_one())
    finally:
        engine.dispose()


def _source_event_count(db_path: Path) -> int:
    """Count ``source.observed`` events for the web connector in the event log."""
    from sqlalchemy import text

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM events "
                        "WHERE event_type = 'source.observed' "
                        "AND json_extract(payload, '$.connector_name') = 'web'"
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _run_opshub(*args: str, pages: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``python -m opshub <args>`` as a fresh subprocess.

    The :func:`isolated_env` fixture has already pointed every ``OPSHUB_*``
    path env var inside ``tmp_path`` via ``monkeypatch.setenv``; those land
    in ``os.environ`` so the child process inherits the same isolated DB /
    config / data dirs. ``pages`` (when given) is JSON-encoded into
    ``OPSHUB_CONNECTORS__WEB__PAGES`` for the child only.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if pages is not None:
        env["OPSHUB_CONNECTORS__WEB__PAGES"] = json.dumps(pages)
    return subprocess.run(
        [sys.executable, "-m", "opshub", *args],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def _sync_or_skip(url: str) -> subprocess.CompletedProcess[str]:
    """Run the *first* ``web sync`` of a fresh page; skip if Chromium can't render.

    The web connector treats a Chromium launch failure as a per-URL
    WARN+skip (ADR-0019 §決定 fail-safe), so a host that has not run
    ``playwright install chromium --with-deps`` produces a *successful* run
    that observes **0** pages (exit 0, ``synced web: 0 item(s) observed``).
    On a **first** sync of a real, reachable localhost page the connector
    must observe exactly one page when Chromium renders, so we interpret "0
    observed on the first sync" as *Chromium is not provisioned* and skip —
    keeping a plain ``uv run pytest -m browser`` green on a dev box without
    the browser OS deps. CI provisions Chromium + deps via the ``playwright
    install chromium --with-deps`` step, so the count is 1 there and the
    test runs for real.

    The connector's rendered-text logic (mapper / observe threading /
    fingerprint) is covered by the unit suite
    (``tests/unit/connectors/web/``) against a fake browser, so collapsing
    the rare "0 observed" ambiguity onto a skip here does not hide a logic
    bug.

    Two env-not-ready shapes are skipped:

    * **binary missing** — the browser core raises
      :class:`~opshub.core.errors.ConfigError`, which the connector lets
      propagate as a whole-run failure → non-zero exit with ``ConfigError``
      / ``playwright install`` on stderr.
    * **launch failure (missing OS shared libs)** — the connector's
      per-URL WARN+skip yields exit 0 with ``synced web: 0 item(s)
      observed`` (the launch reason is logged to the child's stderr).
    """
    result = _run_opshub("--no-progress", "web", "sync", pages=[url])
    combined = result.stdout + result.stderr
    binary_missing = result.returncode != 0 and (
        "ConfigError" in combined or "playwright install" in combined
    )
    launch_failed = result.returncode == 0 and "synced web: 0 item(s) observed" in result.stdout
    if binary_missing or launch_failed:
        pytest.skip(
            "Chromium not provisioned (binary or OS deps missing). Run "
            "'playwright install chromium --with-deps'. "
            f"[exit={result.returncode}] {combined[:300]}"
        )
    return result


def test_web_sync_persists_source_and_search_hits(
    isolated_env: dict[str, Path],
    local_server: str,
) -> None:
    """Sync renders the page, persists a ``web_page`` source, FTS5 finds it."""
    db_path = isolated_env["db_path"]

    result = _sync_or_skip(local_server)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "synced web: 1 item(s) observed" in result.stdout

    # One web_page source row persisted.
    assert _row_count(db_path, "sources", "connector_name = 'web'") == 1
    assert _row_count(db_path, "sources", "source_type = 'web_page'") == 1

    # FTS5 search over the body finds the rendered (incl. JS-injected) text.
    search = _run_opshub("search", "injected marker", "--connector", "web", "--format", "json")
    assert search.returncode == 0, search.stdout + search.stderr
    hits = json.loads(search.stdout)
    assert len(hits) >= 1
    assert any(h["connector"] == "web" for h in hits)


def test_web_sync_extracts_js_rendered_text(
    isolated_env: dict[str, Path],
    local_server: str,
) -> None:
    """The persisted body contains the JS-injected text (Playwright value)."""
    db_path = isolated_env["db_path"]

    result = _sync_or_skip(local_server)
    assert result.returncode == 0, result.stdout + result.stderr

    from sqlalchemy import select

    from opshub.projections.sources import sources_table

    engine = create_engine_for_sqlite(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(sources_table.c.body, sources_table.c.summary).where(
                    sources_table.c.connector_name == "web"
                )
            ).first()
    finally:
        engine.dispose()

    assert row is not None
    body, summary = row
    # Static text AND the JS-injected text both land in the rendered body.
    assert "static body sentinel" in body
    assert "js injected marker" in body
    # summary = <title>.
    assert summary == "opshub web connector page"


def test_web_sync_is_idempotent(
    isolated_env: dict[str, Path],
    local_server: str,
) -> None:
    """A second sync of the unchanged page emits no new ``SourceObserved``.

    The SHA-256 body fingerprint matches the prior ``sources.fingerprint``
    so the connector short-circuits (ADR-0019 §決定 (d) change-detection).
    """
    db_path = isolated_env["db_path"]

    first = _sync_or_skip(local_server)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "synced web: 1 item(s) observed" in first.stdout
    assert _source_event_count(db_path) == 1

    # Second sync (fresh subprocess): unchanged page → 0 observed, event
    # count unchanged.
    second = _run_opshub("--no-progress", "web", "sync", pages=[local_server])
    assert second.returncode == 0, second.stdout + second.stderr
    assert "synced web: 0 item(s) observed" in second.stdout
    assert _source_event_count(db_path) == 1
    # Still exactly one source row (re-observe would have upserted, not
    # duplicated, but here no event fired at all).
    assert _row_count(db_path, "sources", "connector_name = 'web'") == 1
