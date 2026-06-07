"""Web page connector package (Phase 21-C, ADR-0037 / ADR-0010 §Phase 21 改訂).

The ``web`` connector renders each URL the operator lists in
``[connectors.web] pages`` with the Playwright browser core
(:mod:`opshub.browser.core`, Phase 21-B / #506) and persists the rendered
DOM text as a ``web_page`` :class:`~opshub.domain.events.source.SourceObserved`.
It is opshub's first ingestion path that reads content a real browser
engine renders, so JavaScript-driven SPAs / dynamic pages land in
``sources.body`` like every other connector body.

The package follows the FS-connector (box_drive / onedrive_drive) layout —
``fetcher`` (browser driver) + ``mapper`` (``PageContent`` →
``SourceObserved``) + ``connector`` (Protocol impl) — because, like the
local-FS connectors, web has **no delta API**: it detects change via the
:class:`~opshub.domain.events.source.SourceObserved.fingerprint` column
(ADR-0019 §決定 (d) pattern applied to a rendered-text SHA-256, ADR-0010
§Phase 21 改訂 (o)). It is **not a crawler** — only the listed URLs are
fetched, never links found on them (ADR-0010 §Phase 21 改訂 (n)).

Importing this module triggers ``register_connector(WebConnector())`` so
the CLI driver (``opshub web sync``) can discover the connector through
:func:`opshub.connectors.discover_connectors`. Heavy dependencies
(:class:`OpsHubSettings`, the ``sources`` projection, the
``playwright``-backed browser core) are deferred until
:meth:`WebConnector.sync` runs, so this side-effect import stays within the
ADR-0001 ~300 ms cold-start budget (the ``playwright`` import is itself
lazy inside the browser core, ADR-0037 §決定 (g)).
"""

from __future__ import annotations

from opshub.connectors._registry import register_connector
from opshub.connectors.web.connector import WebConnector
from opshub.connectors.web.fetcher import fetch_pages
from opshub.connectors.web.mapper import (
    DEFAULT_ACTOR,
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    fingerprint_body,
    map_page_content,
    normalize_url,
)

__all__ = [
    "DEFAULT_ACTOR",
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "WebConnector",
    "fetch_pages",
    "fingerprint_body",
    "map_page_content",
    "normalize_url",
]

# Register exactly once on first import. The registry's idempotency rule
# (registering the *same* instance twice is a no-op) makes this safe even
# when the package is imported through multiple paths within a single
# process; a *different* instance under the same name would raise — the
# guard against an accidental double-class refactor.
register_connector(WebConnector())
