"""Web page :class:`PageContent` → :class:`SourceObserved` mapper (Phase 21-C, ADR-0037).

The fetcher in :mod:`opshub.connectors.web.fetcher` renders each URL the
operator listed in ``[connectors.web] pages`` with the Playwright browser
core (:func:`opshub.browser.core.fetch_page`) and yields a
:class:`~opshub.browser.core.PageContent` (``url`` / ``title`` / ``text`` /
``truncated``). This module projects each :class:`PageContent` into a
:class:`SourceObserved` event ready for the
:class:`~opshub.connectors.web.connector.WebConnector` to forward through
:meth:`opshub.services.source_service.SourceService.observe`.

Mapping contract (ADR-0010 §Phase 21 改訂 (n)-(o))
--------------------------------------------------

* ``connector_name`` = ``"web"`` — the registry / CLI dispatch key.
* ``source_type`` = :data:`SOURCE_TYPE` (``"web_page"``) — ADR-0037 mints
  a dedicated tag for browser-rendered Web pages, distinct from every
  API / FS connector's ``source_type``.
* ``external_id`` = :func:`normalize_url` of the fetched URL (ADR-0010
  §Phase 21 改訂 (n)). The URL is normalised (scheme / host lowercased,
  fragment stripped, default port dropped) and used verbatim — **no SHA
  hash** — so ``opshub source show <id>`` / recall output / human debug
  stay grep-friendly, mirroring box_drive's ``rel_path`` identity
  (ADR-0019 §決定 (c)).
* ``summary`` = the page ``<title>`` truncated to :data:`SUMMARY_MAX_CHARS`
  (ADR-0005 External Content Minimization, 200-char cap). When the page
  has no ``<title>`` (Playwright returns ``""``), the normalised URL is
  the summary fallback (ADR-0010 §Phase 21 改訂 (n) — "title 不在時は正規化
  URL を summary fallback" / §不変条件 6 title fallback と整合).
* ``title`` = the page ``<title>``, falling back to the normalised URL
  when empty (``SourceObserved.title`` carries ``min_length=1`` so an
  empty ``<title>`` cannot round-trip).
* ``url`` = the normalised URL (the operator-facing canonical address;
  matches ``external_id`` so ``opshub source open`` works).
* ``body`` = the extracted, possibly head-truncated, rendered DOM text
  (:attr:`PageContent.text`). The body is the whole point of the browser
  layer (ADR-0010 §Phase 21 改訂: web は本文取り込みが目的) so we retain it
  in full per ADR-0020 Full Local Content Retention.
* ``fingerprint`` = :func:`fingerprint_body` — the SHA-256 hex digest of
  the extracted body text (ADR-0010 §Phase 21 改訂 (o) #3). box_drive
  hashes stat metadata (``f"{size}:{mtime_ns}"``) because it must not read
  bodies (ADR-0019 §決定 (b)); web has no stat concept and *does* read the
  body, so it hashes the rendered text. An unchanged page produces the
  same digest, so the connector's prior-fingerprint comparison suppresses
  a redundant :class:`SourceObserved` (ADR-0019 §決定 (d) pattern applied
  to web).
* ``actor`` = :data:`DEFAULT_ACTOR` (``"web:browser"``) — the Web page has
  no SaaS user identity (we rendered it with our own headless Chromium),
  so the connector stamps a synthetic actor recognisable in recall output,
  mirroring box_drive's ``box_drive:local`` convention (ADR-0019 §決定 (g)).
* ``provenance_origin`` = ``"external"`` / ``provenance_trust`` =
  ``"untrusted"`` — a rendered Web page is external, attacker-influenceable
  content (indirect prompt injection surface), so the body is tagged
  untrusted for every downstream agent / LLM (ADR-0020 §(e), ADR-0037
  §セキュリティ Web ページ由来 prompt injection).

The mapper is a pure function (no side effects). Constructing the
:class:`SourceObserved` does not persist anything — the connector / service
layer owns that, mirroring the box_drive mapper precedent.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

from opshub.browser.core import PageContent
from opshub.core.ids import new_ulid
from opshub.core.time import now_utc
from opshub.domain.events import SourceObserved

__all__ = [
    "DEFAULT_ACTOR",
    "SOURCE_TYPE",
    "SUMMARY_MAX_CHARS",
    "fingerprint_body",
    "map_page_content",
    "normalize_url",
]


#: ``source_type`` stamped on every :class:`SourceObserved` produced by the
#: web connector. ADR-0037 mints a dedicated tag for browser-rendered Web
#: pages, distinct from every API / FS connector's ``source_type``.
SOURCE_TYPE = "web_page"

#: Hard cap on :attr:`SourceObserved.summary`, mirroring ADR-0005 External
#: Content Minimization. The Pydantic event schema also enforces
#: ``max_length=200``; this constant is the operational knob the mapper
#: truncates against so the Pydantic validator is the safety net, not the
#: primary truncation site (matches the box_drive mapper).
SUMMARY_MAX_CHARS = 200

#: ``actor`` stamped on every web :class:`SourceObserved`. Distinct from any
#: SaaS connector actor so recall queries / audit logs can tell a
#: browser-rendered Web observation apart (ADR-0019 §決定 (g) convention,
#: applied to web).
DEFAULT_ACTOR = "web:browser"

#: Default ports stripped during URL normalisation so ``https://x.com:443/``
#: and ``https://x.com/`` collapse to one ``external_id``.
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_url(url: str) -> str:
    """Return a canonical form of ``url`` for use as ``external_id``.

    The normalisation is intentionally conservative — it only collapses
    forms that unambiguously address the same resource, so two distinct
    pages never alias onto one ``external_id`` (ADR-0010 §Phase 21 改訂 (n)):

    * **scheme + host lowercased** — schemes and host names are
      case-insensitive (RFC 3986 §6.2.2.1), so ``HTTPS://Example.COM/`` and
      ``https://example.com/`` are the same page.
    * **fragment stripped** — ``#section`` is a client-side anchor the
      server never sees; ``…/page#a`` and ``…/page#b`` fetch byte-identical
      responses (ADR-0010 §Phase 21 改訂 (n) fragment 除去).
    * **default port dropped** — ``https://x.com:443/`` ≡ ``https://x.com/``
      (and ``:80`` for http). A non-default port is preserved.
    * **empty path → ``"/"``** — ``https://x.com`` and ``https://x.com/``
      are the same origin root; we canonicalise to the trailing slash so
      they share one row (ADR-0010 §Phase 21 改訂 (n) 末尾スラッシュ規約).

    The query string is preserved verbatim — ``?id=1`` vs ``?id=2`` are
    genuinely different pages, and we cannot know which params are
    significant without per-site knowledge (out of scope; YAGNI). Path case
    is preserved too: path segments are case-sensitive on most servers.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    # ``hostname`` is already lowercased + has the port/userinfo stripped;
    # rebuild ``netloc`` so we control port handling precisely.
    host = parts.hostname or ""
    netloc = host
    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    path = parts.path or "/"
    # Drop the fragment (4th component → "") and forward query verbatim.
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def fingerprint_body(body: str) -> str:
    """Return the SHA-256 hex digest of ``body`` (ADR-0010 §Phase 21 改訂 (o) #3).

    The digest is the web connector's change-detection token: the
    connector compares it against the prior ``sources.fingerprint`` and
    suppresses a :class:`SourceObserved` when it is unchanged (ADR-0019
    §決定 (d) pattern). A stable input text → stable digest, so an
    unchanged page round-trips to the same value and emits no event noise.

    Hashing the *body text* (not raw HTML / response bytes) means dynamic
    chrome the render strips (``<script>`` / ``<style>``) does not perturb
    the digest, while genuine content changes do — the change-detection
    semantics the agent cares about (ADR-0010 §Phase 21 改訂 (o) #5 false
    positive 受容).
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def map_page_content(
    page: PageContent,
    *,
    actor: str = DEFAULT_ACTOR,
) -> SourceObserved:
    """Project ``page`` into a :class:`SourceObserved` event.

    Parameters
    ----------
    page:
        A :class:`~opshub.browser.core.PageContent` yielded by
        :func:`opshub.connectors.web.fetcher.fetch_pages`. The fetcher
        forwards the *input* URL on :attr:`PageContent.url` (the browser
        core does not normalise); this mapper owns URL normalisation per
        ADR-0010 §Phase 21 改訂 (n).
    actor:
        Override for :attr:`SourceObserved.actor`. Defaults to
        :data:`DEFAULT_ACTOR` so unit tests can drive the mapper in
        isolation without re-specifying the convention every time.

    Returns
    -------
    SourceObserved
        A frozen Pydantic event ready for append through
        :meth:`SourceService.observe`. The mapper does NOT persist it —
        the caller (:class:`WebConnector`) routes the projection through
        the service layer so the accompanying
        :class:`~opshub.domain.events.inbox.ItemEnqueued` + atomic-UoW
        guarantees come along.
    """
    normalized = normalize_url(page.url)
    # ``<title>`` empty → fall back to the normalised URL for both summary
    # and title (ADR-0010 §Phase 21 改訂 (n) title 不在時 URL fallback;
    # ``SourceObserved.title`` requires ``min_length=1``).
    title = page.title.strip() or normalized
    summary = _truncate_summary(title)
    fingerprint = fingerprint_body(page.text)
    return SourceObserved(
        aggregate_id=new_ulid(),
        occurred_at=now_utc(),
        actor=actor,
        connector_name="web",
        external_id=normalized,
        source_type=SOURCE_TYPE,
        title=title,
        url=normalized,
        summary=summary,
        fingerprint=fingerprint,
        body=page.text,
        # A rendered Web page is external, attacker-influenceable content
        # (indirect prompt injection surface), so the body is tagged
        # untrusted for every downstream agent / LLM (ADR-0020 §(e)).
        provenance_origin="external",
        provenance_trust="untrusted",
    )


def _truncate_summary(text: str) -> str:
    """Return ``text`` truncated to :data:`SUMMARY_MAX_CHARS` with an ellipsis.

    Truncation appends a trailing ``"…"`` so the cap is visible to
    operators reading recall output — without the ellipsis a long title
    silently looks complete. The final string length is exactly
    :data:`SUMMARY_MAX_CHARS` (matches the box_drive mapper's contract).
    """
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    # Reserve one character for the ellipsis so the final string is exactly
    # :data:`SUMMARY_MAX_CHARS` long.
    return text[: SUMMARY_MAX_CHARS - 1] + "…"
