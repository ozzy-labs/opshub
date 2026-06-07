"""Tests for :mod:`opshub.connectors.web.mapper` (Phase 21-C, ADR-0037).

Pins the mapper contract (ADR-0010 §Phase 21 改訂 (n)-(o)) independently of
the connector / CLI lifecycle:

1. URL normalisation (scheme / host lowercase, fragment strip, default
   port drop, empty path → ``"/"``, query preserved).
2. ``map_page_content`` field shapes (``connector_name`` / ``source_type``
   / ``external_id`` = normalised URL / ``summary`` = title / ``body`` =
   text / provenance tags).
3. ``<title>`` empty → normalised URL used as both ``title`` and
   ``summary`` fallback.
4. ``summary`` truncation at 200 chars with a trailing ellipsis.
5. ``fingerprint_body`` = SHA-256 hex of the body, stable across calls.
"""

from __future__ import annotations

import hashlib

import pytest

from opshub.browser.core import PageContent
from opshub.connectors.web.mapper import (
    DEFAULT_ACTOR,
    SOURCE_TYPE,
    SUMMARY_MAX_CHARS,
    fingerprint_body,
    map_page_content,
    normalize_url,
)


def _page(
    *,
    url: str = "https://example.com/page",
    title: str = "Example Page",
    text: str = "rendered body text",
    truncated: bool = False,
) -> PageContent:
    return PageContent(url=url, title=title, text=text, truncated=truncated)


# ---------------------------------------------------------------- normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # scheme + host lowercased
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        # fragment stripped (client-side anchor the server never sees)
        ("https://example.com/page#section", "https://example.com/page"),
        # default https port dropped
        ("https://example.com:443/page", "https://example.com/page"),
        # default http port dropped
        ("http://example.com:80/page", "http://example.com/page"),
        # non-default port preserved
        ("https://example.com:8443/page", "https://example.com:8443/page"),
        # empty path → "/"
        ("https://example.com", "https://example.com/"),
        ("https://example.com#x", "https://example.com/"),
        # query preserved verbatim (genuinely different pages)
        ("https://example.com/s?q=1", "https://example.com/s?q=1"),
        # path case preserved (path segments are case-sensitive)
        ("https://example.com/CaseSensitive", "https://example.com/CaseSensitive"),
        # surrounding whitespace stripped
        ("  https://example.com/page  ", "https://example.com/page"),
    ],
)
def test_normalize_url_canonicalises(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_is_idempotent() -> None:
    """Normalising an already-normalised URL is a no-op (stable external_id)."""
    once = normalize_url("HTTPS://Example.com:443/p#frag")
    assert normalize_url(once) == once


# ------------------------------------------------------------ fingerprint_body


def test_fingerprint_body_is_sha256_hex() -> None:
    body = "rendered body text"
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert fingerprint_body(body) == expected


def test_fingerprint_body_stable_across_calls() -> None:
    """Same input → same digest (idempotent re-sync depends on this)."""
    assert fingerprint_body("same") == fingerprint_body("same")


def test_fingerprint_body_changes_with_content() -> None:
    assert fingerprint_body("a") != fingerprint_body("b")


# --------------------------------------------------------- map_page_content


def test_map_page_content_field_shapes() -> None:
    """Pin every field the connector forwards through ``observe``."""
    event = map_page_content(_page(url="HTTPS://Example.COM/page#top"))

    assert event.connector_name == "web"
    assert event.source_type == SOURCE_TYPE == "web_page"
    # external_id is the *normalised* URL, not the raw input.
    assert event.external_id == "https://example.com/page"
    assert event.url == "https://example.com/page"
    assert event.title == "Example Page"
    assert event.summary == "Example Page"
    assert event.body == "rendered body text"
    assert event.fingerprint == fingerprint_body("rendered body text")
    assert event.actor == DEFAULT_ACTOR == "web:browser"
    # External, attacker-influenceable content → untrusted (ADR-0020 §(e)).
    assert event.provenance_origin == "external"
    assert event.provenance_trust == "untrusted"


def test_map_page_content_empty_title_falls_back_to_url() -> None:
    """No ``<title>`` → normalised URL used for both title and summary."""
    event = map_page_content(_page(url="https://example.com/no-title", title=""))

    assert event.title == "https://example.com/no-title"
    assert event.summary == "https://example.com/no-title"


def test_map_page_content_whitespace_title_falls_back_to_url() -> None:
    """A whitespace-only ``<title>`` is treated as absent (URL fallback)."""
    event = map_page_content(_page(url="https://example.com/p", title="   "))

    assert event.title == "https://example.com/p"
    assert event.summary == "https://example.com/p"


def test_map_page_content_truncates_long_summary() -> None:
    """A title past 200 chars is truncated to exactly 200 with an ellipsis."""
    long_title = "T" * 500
    event = map_page_content(_page(title=long_title))

    assert event.summary is not None
    assert len(event.summary) == SUMMARY_MAX_CHARS == 200
    assert event.summary.endswith("…")
    # title is NOT truncated (schema cap is 500) — only summary.
    assert event.title == long_title


def test_map_page_content_actor_override() -> None:
    """The actor override lets tests drive the mapper without the default."""
    event = map_page_content(_page(), actor="web:custom")
    assert event.actor == "web:custom"


def test_map_page_content_occurred_at_is_tz_aware() -> None:
    """``occurred_at`` is set to the observation time (tz-aware, UTC)."""
    event = map_page_content(_page())
    assert event.occurred_at.tzinfo is not None
