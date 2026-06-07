"""Web page fetcher — drives the browser core over a list of URLs (Phase 21-C).

Thin orchestration layer between the :class:`WebConnector` and the
Playwright browser core (:func:`opshub.browser.core.fetch_page`). It
iterates ``[connectors.web] pages``, renders each URL, and yields a
:class:`~opshub.browser.core.PageContent` per **successful** fetch.

Per-URL failure posture (issue #507 / ADR-0010 §禁止事項 + ADR-0019 §決定)
------------------------------------------------------------------------

A single dead / slow URL must not abort the whole sync — the other
operator-listed pages should still be observed. So a
:class:`~opshub.browser.core.BrowserFetchError` (the browser core's
navigation / timeout / render failure, a
:class:`~opshub.core.errors.ConnectorFailedError` subclass) is logged at
**WARN** and the loop **continues** to the next URL. The error message is
already sanitised at the browser core's raise site
(:func:`opshub.core.sanitise.sanitise_error_message`), so a URL with an
embedded credential never reaches the log.

A :class:`~opshub.core.errors.ConfigError` (missing ``playwright`` package
or un-provisioned Chromium binary) is **not** swallowed — it is a
whole-run misconfiguration (every URL would fail identically), so it
propagates so the CLI driver can surface the ``playwright install
chromium`` pointer and exit non-zero without writing a per-URL
``ConnectorSyncFailed`` event. The connector catches it before sync starts
(see :meth:`WebConnector.sync`).

The browser core's ``playwright`` import is lazy (ADR-0037 §決定 (g)), so
this module — and the connector that imports it — stay import-light: the
``[browser]`` extra never reaches the ``opshub --help`` cold-start path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opshub.browser.core import BrowserFetchError, fetch_page

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from opshub.browser.core import PageContent
    from opshub.core.config import OpsHubSettings

__all__ = ["fetch_pages"]


def fetch_pages(
    pages: Iterable[str],
    *,
    settings: OpsHubSettings,
    logger: object,
) -> Iterator[PageContent]:
    """Render each URL in ``pages`` and yield one :class:`PageContent` per success.

    Parameters
    ----------
    pages:
        The operator-listed URLs (``[connectors.web] pages``). Fetched in
        list order; each is forwarded to
        :func:`opshub.browser.core.fetch_page` verbatim (the mapper owns
        normalisation, ADR-0010 §Phase 21 改訂 (n)).
    settings:
        The resolved :class:`~opshub.core.config.OpsHubSettings`. The
        ``[browser]`` section supplies headless / channel / timeout /
        cdp_endpoint and ``data_dir`` resolves the dedicated
        ``user-data-dir`` for the launch path.
    logger:
        A ``structlog`` bound logger (typed ``object`` to keep this module
        import-light — it is only ever ``.warning(...)``-ed). A failed
        fetch is logged here at WARN with the sanitised reason.

    Yields
    ------
    PageContent
        One per URL that fetched + rendered successfully. URLs that raise
        :class:`~opshub.browser.core.BrowserFetchError` are skipped (logged
        at WARN). :class:`~opshub.core.errors.ConfigError` is **not**
        caught — it propagates as a whole-run failure (binary missing).
    """
    for url in pages:
        try:
            yield fetch_page(url, settings=settings)
        except BrowserFetchError as exc:
            # Per-URL skip-and-continue: one dead page does not abort the
            # run (ADR-0019 §決定 fail-safe posture). The message is
            # already sanitised at the browser core's raise site, so no
            # token / URL-embedded secret leaks into the log line. We log
            # ``str(exc)`` (the sanitised reason) rather than re-deriving
            # it, and pass ``url`` as a structured field so operators can
            # grep the WARN line back to the offending page.
            _warn(logger, url, str(exc))


def _warn(logger: object, url: str, reason: str) -> None:
    """Emit a WARN line for a skipped URL, tolerant of stub loggers.

    The production logger is a ``structlog`` bound logger whose
    ``warning`` accepts ``event`` + arbitrary kwargs. Unit tests may pass a
    ``MagicMock`` (any signature) or a plain object lacking ``warning``; we
    duck-type via ``getattr`` so the fetcher never raises *because of the
    logger* while a URL is being skipped.
    """
    warning = getattr(logger, "warning", None)
    if callable(warning):
        warning("web.fetch_failed", url=url, reason=reason)
