"""Playwright-backed browser page fetch (Phase 21-B, ADR-0037).

This module is the browser read layer's **core**: it renders a Web page
with Chromium (via Playwright's **sync API**, ADR-0037 §決定 (h)) and
extracts the post-render DOM text + ``<title>`` into a small
:class:`PageContent` value object the Phase 21-C ``web`` connector and
the Phase 21-D MCP ``browser.fetch`` tool both consume.

Design decisions pinned by ADR-0037
------------------------------------

* **§決定 (a)/(c)** — Chromium only, headless by default, launched
  against an opshub-dedicated ``user-data-dir``
  (:func:`opshub.core.platform.browser_user_data_dir`). The operator's
  everyday Chrome profile is never touched.
* **§決定 (b)** — when ``[browser] cdp_endpoint`` is set, attach to an
  operator-launched Chrome via ``connect_over_cdp`` instead of
  launching. Raw-WebSocket CDP is never used; the only low-level escape
  hatch is Playwright's own ``connect_over_cdp`` / ``new_cdp_session``.
* **§決定 (d)** — extraction = rendered DOM → text. The concrete means
  this PR settles on is :meth:`Page.inner_text` over ``"body"``
  (Playwright-native, lightweight, returns exactly the *rendered*
  visible text — the JS-described value of using a real browser). We
  deliberately do **not** route through ``markitdown``: that path would
  re-parse static HTML structure (losing the render benefit) and couple
  the browser layer to the ``[office]`` extras' heavy converter. The
  500K char cap + head-truncation marker reuse
  :func:`opshub.core.text_limits.truncate_with_marker`, sharing the
  exact ADR-0025 §決定 (b-2) arithmetic with the Office extractor.
* **§決定 (g)** — the ``playwright`` import is **lazy** (inside the
  function body) so the ``[browser]`` extras never reach the
  ``opshub --help`` cold-start path (ADR-0001). A missing ``playwright``
  package, or a Chromium binary that has not been provisioned, surfaces
  as a :class:`~opshub.core.errors.ConfigError` pointing the operator at
  ``playwright install chromium`` — symmetric with the ``box_drive``
  root-missing ``ConfigError`` (ADR-0037 §決定 (g)).
* **§決定 (h)** — sync API. The MCP async handler bridges in via
  ``asyncio.to_thread`` (Phase 21-D); this module stays synchronous so
  it matches the CLI-first / sync-SQLAlchemy codebase.

Error contract
--------------

Unlike :func:`opshub.core.document_extract.extract_document` (which
never raises so an FS scan has a single happy path), :func:`fetch_page`
**does** raise — a single page is the whole unit of work here, so a
failed render is a failed call, not a skip-and-continue. Every failure
is converted to an :class:`~opshub.core.errors.OpsHubError` subclass:

* missing package / Chromium binary → :class:`ConfigError`
  (actionable: run ``playwright install chromium``).
* navigation error / timeout / render failure →
  :class:`BrowserFetchError` (a :class:`ConnectorFailedError` subclass
  so the Phase 21-C connector's existing ``ConnectorFailedError``
  fail-safe path treats a single dead URL as a per-page skip without a
  new ``except`` clause). The message is run through
  :func:`opshub.core.sanitise.sanitise_error_message` so a URL with an
  embedded token never leaks into a log or persisted body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from opshub.core.config import OpsHubSettings
from opshub.core.errors import ConfigError, ConnectorFailedError
from opshub.core.logging import get_logger
from opshub.core.platform import browser_user_data_dir
from opshub.core.sanitise import sanitise_error_message
from opshub.core.text_limits import truncate_with_marker

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext

__all__ = [
    "DEFAULT_MAX_CHARS",
    "BrowserFetchError",
    "PageContent",
    "fetch_page",
]

logger = get_logger(__name__)


#: Character cap on the extracted page text, reusing ADR-0025 §決定
#: (b-2)'s 500K ceiling verbatim (ADR-0037 §決定 (d): "browser 専用に
#: 別 default を切らない"). Imported from
#: :data:`opshub.core.document_extract.DEFAULT_MAX_CHARS` would create a
#: ``core.document_extract`` → ``markitdown``-adjacent import edge for a
#: single int, so we re-pin the literal here and keep a value-pin test
#: in lockstep instead.
DEFAULT_MAX_CHARS: Final[int] = 500_000


#: Marker appended when the extracted page text exceeds ``max_chars``.
#: Distinct wording from the Office extractor's marker so a downstream
#: parser can tell a browser-truncated body apart from an Office one,
#: but the ``{kept}`` / ``{original}`` placeholders match
#: :func:`opshub.core.text_limits.truncate_with_marker`'s contract.
_BROWSER_TRUNCATION_MARKER: Final[str] = (
    "\n\n[browser body truncated: kept {kept} / {original} chars]"
)


class BrowserFetchError(ConnectorFailedError):
    """A browser page fetch failed (navigation error / timeout / render).

    Subclasses :class:`~opshub.core.errors.ConnectorFailedError` so the
    Phase 21-C ``web`` connector's existing ``ConnectorFailedError``
    fail-safe (per-page skip, ``ConnectorSyncFailed`` rendering) catches
    it without a new ``except`` clause. The message is sanitised at the
    raise site (no token / URL-embedded secret leaks). Distinct from the
    binary-missing case, which is a :class:`~opshub.core.errors.ConfigError`
    (operator-fixable misconfiguration, not a transient page failure).
    """


@dataclass(frozen=True, slots=True)
class PageContent:
    """Result of one :func:`fetch_page` call.

    Attributes
    ----------
    url:
        The URL that was fetched (the input URL, unchanged — the
        connector owns URL normalisation per ADR-0010 §Phase 21 改訂).
    title:
        The page ``<title>`` after render, or ``""`` when the page has
        no title element (Playwright returns an empty string, not
        ``None``). The Phase 21-C connector maps this to
        ``SourceObserved.summary``.
    text:
        The extracted, possibly head-truncated, rendered DOM text
        (``page.inner_text("body")``). Maps to ``SourceObserved.body``.
    truncated:
        ``True`` when the rendered text exceeded ``max_chars`` and was
        head-truncated (the marker is already appended to :attr:`text`).
    """

    url: str
    title: str
    text: str
    truncated: bool


def _launch_context(
    settings: OpsHubSettings,
) -> tuple[BrowserContext, Browser | None]:
    """Open a Playwright browser context per the ``[browser]`` settings.

    Returns ``(context, browser)`` where ``browser`` is non-``None``
    only on the ``connect_over_cdp`` attach path (the persistent-context
    launch path owns its own browser internally, so it returns ``None``
    and the caller closes the context alone). Splitting the two shapes
    here keeps :func:`fetch_page`'s ``finally`` cleanup uniform.

    The ``playwright`` import is deferred to the call site (this helper
    is only reached from inside :func:`fetch_page`'s already-deferred
    import scope). Raises :class:`ConfigError` when the Chromium binary
    is missing.
    """
    # Lazy import — ADR-0037 §決定 (g). Keeping the import inside the
    # function body means the ``[browser]`` extras never reach the
    # ``opshub --help`` cold-start path (ADR-0001).
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ConfigError(
            "browser support requires the 'browser' extra. Install it with "
            "'uv pip install opshub[browser]' (or 'uv sync --extra browser'), "
            "then provision Chromium with 'playwright install chromium'."
        ) from exc

    playwright = sync_playwright().start()
    try:
        cdp_endpoint = settings.browser.cdp_endpoint
        if cdp_endpoint is not None:
            # ADR-0037 §決定 (b) escape hatch: attach to an
            # operator-launched Chrome already listening on a
            # ``--remote-debugging-port``. The persistent profile /
            # headless / channel knobs do not apply — the operator owns
            # the launched browser. We reuse its first context (Chrome
            # always exposes a default context) rather than creating a
            # new one so cookies / auth state the operator established
            # are visible (the reserved authenticated-session path).
            browser = playwright.chromium.connect_over_cdp(cdp_endpoint)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            return context, browser

        # Launch path (ADR-0037 §決定 (c)): an opshub-dedicated
        # persistent context so cookies / cache live under our data dir,
        # never the operator's everyday profile.
        user_data_dir = browser_user_data_dir(settings.data_dir)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=settings.browser.headless,
            channel=settings.browser.channel,
        )
        return context, None
    except Exception as exc:
        # Stop the Playwright driver before re-raising so we do not leak
        # the subprocess. A missing Chromium binary surfaces here as a
        # ``playwright`` ``Error`` whose message names the install
        # command; convert it to an actionable ``ConfigError``.
        playwright.stop()
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            raise ConfigError(
                "Chromium is not installed for Playwright. Run "
                "'playwright install chromium' to provision the browser binary "
                "(ADR-0037: binary distribution is an operator step)."
            ) from exc
        raise BrowserFetchError(
            f"failed to launch browser: {sanitise_error_message(message)}"
        ) from exc


def fetch_page(
    url: str,
    *,
    settings: OpsHubSettings,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> PageContent:
    """Render ``url`` with Chromium and extract its text + title.

    Navigates to ``url``, waits for the page ``load`` event, then reads
    the rendered DOM text (``page.inner_text("body")``) and ``<title>``.
    The text is head-truncated to ``max_chars`` (default 500K, ADR-0025
    §決定 (b-2) discipline) with a marker appended.

    Parameters
    ----------
    url:
        Absolute URL to fetch. The caller (connector / MCP tool) owns
        URL validation / normalisation; this function forwards it to
        ``page.goto`` verbatim.
    settings:
        The resolved :class:`~opshub.core.config.OpsHubSettings`. The
        ``[browser]`` section supplies headless / channel / timeout /
        cdp_endpoint, and ``data_dir`` resolves the dedicated
        ``user-data-dir``.
    max_chars:
        Head-truncate the extracted text at this many characters.
        Defaults to :data:`DEFAULT_MAX_CHARS`. ``0`` disables the cap
        (mirrors the ``truncate_with_marker`` "0 = unlimited"
        convention; discouraged for the same reason as the Office
        extractor).

    Returns
    -------
    PageContent
        ``url`` / ``title`` / ``text`` / ``truncated``.

    Raises
    ------
    ConfigError
        The ``playwright`` package or the Chromium binary is missing —
        the message names ``playwright install chromium``.
    BrowserFetchError
        Navigation failed / timed out / the render raised. The message
        is sanitised so no token shape (e.g. a URL-embedded credential)
        leaks. The Phase 21-C connector's ``ConnectorFailedError``
        fail-safe path catches it as a per-page skip.
    """
    # ``PlaywrightError`` / ``PlaywrightTimeoutError`` are imported here
    # (deferred, ADR-0037 §決定 (g)) so the ``except`` clauses can name
    # the concrete Playwright exception types without a module-level
    # ``playwright`` import. The package-missing case is already handled
    # by :func:`_launch_context` raising ``ConfigError`` before we reach
    # the ``page.goto`` call, so import failure here is unreachable in
    # practice — but we still gate it for type-checker completeness.
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:  # pragma: no cover - guarded by _launch_context
        raise ConfigError(
            "browser support requires the 'browser' extra. Install it with "
            "'uv pip install opshub[browser]' (or 'uv sync --extra browser'), "
            "then provision Chromium with 'playwright install chromium'."
        ) from exc

    context, browser = _launch_context(settings)
    try:
        page = context.new_page()
        timeout = settings.browser.timeout
        try:
            page.goto(url, wait_until="load", timeout=timeout)
            title = page.title()
            raw_text = page.inner_text("body")
        except PlaywrightTimeoutError as exc:
            raise BrowserFetchError(
                f"timed out loading page after {timeout}ms: {sanitise_error_message(str(exc))}"
            ) from exc
        except PlaywrightError as exc:
            raise BrowserFetchError(
                f"failed to fetch page: {sanitise_error_message(str(exc))}"
            ) from exc
    finally:
        # ``launch_persistent_context`` owns its browser internally, so
        # closing the context tears it down. The ``connect_over_cdp``
        # path returns a separate ``browser`` handle we close too — but
        # the operator-launched Chrome itself keeps running (we only
        # detach), which is the intended escape-hatch behaviour.
        context.close()
        if browser is not None:
            browser.close()

    text, truncated = truncate_with_marker(
        raw_text,
        max_chars=max_chars,
        marker_template=_BROWSER_TRUNCATION_MARKER,
    )
    logger.info(
        "browser.fetch_page",
        url=url,
        title_len=len(title),
        text_len=len(text),
        truncated=truncated,
    )
    return PageContent(url=url, title=title, text=text, truncated=truncated)
