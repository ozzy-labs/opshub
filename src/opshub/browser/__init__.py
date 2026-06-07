"""Browser read layer (Phase 21, epic #504, ADR-0037).

The browser package is opshub's first ingestion path that renders a Web
page with a real browser engine (Chromium via Playwright) and extracts
its **post-render DOM text**, so JavaScript-driven SPAs / dynamic pages
land in ``sources.body`` like every other connector body.

Phase 21-B (#506) ships the **core module** only
(:mod:`opshub.browser.core`): launch / ``connect_over_cdp`` attach,
page fetch (navigate → wait → extract text + ``<title>``), the 500K
char cap (ADR-0025 §決定 (b-2) discipline reused), and timeout /
navigation-error → :class:`~opshub.core.errors.OpsHubError` conversion.

Downstream phases consume this core:

* **21-C (#507)** — the ``web`` connector (``opshub web sync``) maps a
  fetched page to a ``web_page`` ``SourceObserved``.
* **21-D (#508)** — the MCP ``browser.fetch`` tool (write-category,
  ADR-0037 §決定 (e)) bridges into this sync core via
  ``asyncio.to_thread`` (ADR-0037 §決定 (h)).

The package follows ADR-0001: the ``playwright`` import is **lazy**
(inside function bodies) so the ``[browser]`` extras never leak onto the
``opshub --help`` cold-start path even when installed, and a missing
``playwright`` package / Chromium binary surfaces as a
:class:`~opshub.core.errors.ConfigError` that points the operator at
``playwright install chromium`` (ADR-0037 §決定 (g)).
"""

from __future__ import annotations

from opshub.browser.core import (
    DEFAULT_MAX_CHARS,
    PageContent,
    fetch_page,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "PageContent",
    "fetch_page",
]
