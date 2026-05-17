"""ASCII-safe slug helper for filenames (Phase 5 step B4).

Converts a free-form string (briefing topic, etc.) to a filesystem-safe
slug suitable for filenames. Strips non-ASCII characters, replaces
whitespace + punctuation with hyphens, collapses runs, lowercases.

Scope (intentionally narrow)
----------------------------

This helper is the minimum viable slug routine for ``opshub brief
--save`` filenames: take a free-form topic, return something that
``Path.write_text`` can write on every supported filesystem (Linux /
macOS / Windows). It is **not** an i18n-aware transliterator — the
NFKD-then-ASCII-encode pass drops every character that decomposes to
non-ASCII (so ``"こんにちは world"`` becomes ``"world"``). The filename
collision risk is mitigated by suffixing every brief filename with the
:class:`Briefing.briefing_id` ULID at the call site; the slug only
needs to be human-readable, not unique.

Fallback (``"briefing"``)
-------------------------

When the input is empty or contains only non-ASCII / punctuation, the
slug returns the stable string ``"briefing"`` rather than an empty
filename. The CLI still appends ``-<briefing_id>`` so the final
filename never collides; the fallback only guarantees the slug portion
is non-empty.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["slugify"]


_FALLBACK_SLUG = "briefing"

# Matches runs of any character that is not an ASCII alphanumeric. The
# replacement target is ``-``; consecutive non-alphanumeric characters
# collapse into a single hyphen because the regex is greedy.
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")


def slugify(text: str, *, max_length: int = 60) -> str:
    """Convert ``text`` to a filename-safe slug.

    Pipeline:

    1. NFKD-normalise the input so Latin-1 accented chars decompose
       (``"naïve"`` → ``"naive"`` after the ASCII-encode pass).
    2. Encode to ASCII with ``errors="ignore"`` — every codepoint that
       did not survive normalisation is dropped (Japanese / CJK /
       emoji etc.).
    3. Collapse runs of non-alphanumeric chars into a single hyphen.
    4. Strip leading / trailing hyphens, lower-case, truncate to
       ``max_length``.
    5. After truncation, strip a possibly-orphaned trailing hyphen
       (truncation might land mid-word and leave one behind).
    6. If the result is empty at any point, return
       :data:`_FALLBACK_SLUG`.

    The hard ``max_length`` cap is 60 chars by default. Combined with
    the 26-char ULID suffix the CLI appends (``-<briefing_id>``) and a
    ``.md`` extension, a brief filename stays comfortably under the
    255-byte POSIX limit + the 260-char Windows MAX_PATH ceiling
    (when the workspace root path is reasonably short).
    """
    if not text:
        return _FALLBACK_SLUG
    normalised = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    cleaned = _NON_ALNUM.sub("-", normalised).strip("-").lower()
    if not cleaned:
        return _FALLBACK_SLUG
    truncated = cleaned[:max_length].rstrip("-")
    return truncated or _FALLBACK_SLUG
