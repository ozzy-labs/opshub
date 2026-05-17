"""Tests for :func:`opshub.core.slug.slugify` (Phase 5 step B4).

The slug helper is the minimum viable filename-safety routine for the
``opshub brief --save`` path. These tests pin the contract the brief
CLI relies on:

* ASCII lower-cased + hyphenated output for ordinary inputs.
* Punctuation and whitespace collapse into single hyphens.
* Non-ASCII characters are stripped (NFKD-then-ASCII pipeline).
* An empty / all-non-alphanumeric input falls back to a stable
  ``"briefing"`` token rather than producing an empty filename.
* ``max_length`` truncates without leaving a trailing hyphen.
"""

from __future__ import annotations

from opshub.core.slug import slugify


def test_slugify_basic_ascii() -> None:
    """A plain ASCII phrase becomes lowercase + hyphen-joined."""
    assert slugify("Hello World") == "hello-world"


def test_slugify_strips_punctuation() -> None:
    """Runs of non-alphanumeric chars collapse into a single hyphen."""
    assert slugify("foo!!! bar?") == "foo-bar"


def test_slugify_collapses_multiple_whitespace() -> None:
    """Multiple spaces / tabs do not produce multiple hyphens."""
    assert slugify("a   b\t\tc") == "a-b-c"


def test_slugify_strips_leading_trailing_hyphens() -> None:
    """A leading / trailing run of punctuation does not bleed through."""
    assert slugify("---hello---") == "hello"


def test_slugify_strips_non_ascii() -> None:
    """Non-ASCII characters drop after NFKD-then-ASCII normalisation.

    ``こんにちは`` decomposes to characters that are not ASCII, so they
    are dropped; only the surviving ASCII portion remains.
    """
    assert slugify("こんにちは world") == "world"


def test_slugify_keeps_ascii_compatible_accented_letters() -> None:
    """Latin-1 accented letters decompose to their ASCII base.

    NFKD normalisation splits ``é`` into ``e`` + combining acute,
    and the ASCII encode pass drops the combining mark. The base
    letter survives, so ``café`` becomes ``cafe`` (not ``caf``).
    """
    assert slugify("café résumé") == "cafe-resume"


def test_slugify_empty_returns_fallback() -> None:
    """An empty string falls back to the stable ``briefing`` token."""
    assert slugify("") == "briefing"


def test_slugify_only_punctuation_returns_fallback() -> None:
    """A string with no alphanumerics falls back to ``briefing``."""
    assert slugify("!!!") == "briefing"


def test_slugify_only_non_ascii_returns_fallback() -> None:
    """A purely non-ASCII string falls back after the stripping pass."""
    assert slugify("こんにちは") == "briefing"


def test_slugify_truncates_to_max_length() -> None:
    """Long inputs truncate at ``max_length`` (default 60)."""
    long_input = "a" * 100
    result = slugify(long_input)
    assert len(result) == 60
    assert result == "a" * 60


def test_slugify_truncation_strips_trailing_hyphen() -> None:
    """Truncation never leaves a dangling trailing hyphen.

    The slugified intermediate ``"abc-defghij..."`` truncated at a
    hyphen boundary would otherwise emit ``"abc-"``; the rstrip pass
    keeps the result clean.
    """
    # 4 chars of content + a hyphen, max_length=5 → "abc-d" trimmed of
    # any trailing hyphen. Use a string where the truncation point
    # lands ON a hyphen to assert the rstrip is exercised.
    result = slugify("abc " * 5, max_length=4)
    assert not result.endswith("-")


def test_slugify_truncation_to_zero_falls_back() -> None:
    """A ``max_length`` that strips everything still returns a fallback.

    Defensive: a future caller could pass ``max_length=0``; the helper
    must never return an empty string (would break filename
    construction).
    """
    assert slugify("hello world", max_length=0) == "briefing"


def test_slugify_custom_max_length() -> None:
    """``max_length`` is honoured when passed explicitly."""
    assert slugify("abcdefghij", max_length=5) == "abcde"
