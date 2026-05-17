"""Workspace inbox markdown ingest (Phase 3 step C1).

Parse hand-written ``workspace/inbox/*.md`` files into structured
:class:`InboxItemDraft` values ready for handoff to
:class:`opshub.services.file_ingest_service.FileIngestService`
(C2) which builds :class:`ItemEnqueued` events via the existing
:class:`opshub.services.inbox_service.InboxService`.

The parser is **pure** (no I/O beyond reading the file path it is
given, no DB / network) so it is trivially unit-testable. C2 owns the
side-effecting orchestration (scan dir + skip already-ingested + emit
events).

Two front-matter shapes are supported:

1. YAML-style fenced front-matter (the conventional Markdown front-matter)::

       ---
       summary: Review PR #99
       source_ref: github:owner/repo#99
       ---

       Optional body...

2. No front-matter — fall back to the first H1 heading, then to the
   filename stem (with hyphens / underscores → spaces, no extension).

The body (everything after the front-matter, if present) is currently
captured but not used by C2 (Inbox items are summary-only per Phase 2
``inbox_items`` schema). It is captured here so a future Phase 3.x or
Phase 4 can repurpose it without re-parsing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["InboxItemDraft", "compute_file_hash", "parse_inbox_file"]


@dataclass(frozen=True, slots=True)
class InboxItemDraft:
    """Pure-data representation of one ``workspace/inbox/*.md`` file.

    Attributes
    ----------
    path:
        Source file path (absolute or relative — caller's choice).
        Used by the service layer for state-tracking; NOT used as an
        event payload.
    summary:
        1..500 chars (clamped by domain validation in C2 — the parser
        does not enforce length, just extracts).
    source_ref:
        Optional ``"<connector_name>:<external_id>"`` style reference.
        Filled in when the front-matter has a ``source_ref`` key.
    body:
        Optional body text after the front-matter. Stripped of
        leading/trailing whitespace; ``None`` when the body is empty
        or absent.
    content_hash:
        SHA-256 of the file's raw bytes (hex). Idempotency key used
        by C2 to skip re-ingest of unchanged files.
    """

    path: Path
    summary: str
    source_ref: str | None
    body: str | None
    content_hash: str


def compute_file_hash(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path``'s raw bytes.

    Used as the idempotency key for the ``ingested_files`` projection
    (C2). Hashing raw bytes (not parsed content) means a whitespace-only
    edit still re-ingests; we accept that — the file changed, the user
    may have intended new semantics.

    Streams the file in 8 KiB chunks so multi-MB inbox files do not
    require loading the whole payload into memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


_FRONT_MATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)",
    re.DOTALL,
)
"""Capture YAML-style front-matter: opening ``---`` on the first line,
closing ``---`` on its own line, group 1 = the body between them,
group 2 = everything that follows (may be empty).

Tolerates a missing trailing newline after the closing ``---`` so a
file that ends immediately after the front-matter still parses.
"""

_HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
"""Capture the first Markdown H1 heading (``# text``).

Multiline mode so ``^`` matches at line boundaries — the heading does
not need to be on line 1, just the first heading anywhere in the text.
"""


def parse_inbox_file(path: Path) -> InboxItemDraft:
    """Parse a single ``workspace/inbox/*.md`` file into a draft.

    Resolution order for ``summary``:

    1. ``summary:`` key in YAML-style front-matter, if present.
    2. First Markdown H1 heading in the body (or whole file if no
       front-matter).
    3. The file's stem with ``-`` / ``_`` rewritten to spaces.
    """
    raw = path.read_text(encoding="utf-8")
    front_matter, body = _split_front_matter(raw)
    fields = _parse_simple_yaml(front_matter) if front_matter is not None else {}
    summary = (
        fields.get("summary")
        or _first_heading(body if body is not None else raw)
        or _summary_from_filename(path)
    )
    source_ref = fields.get("source_ref")
    body_stripped = body.strip() if body else ""
    return InboxItemDraft(
        path=path,
        summary=summary.strip(),
        source_ref=source_ref.strip() if source_ref else None,
        body=body_stripped or None,
        content_hash=compute_file_hash(path),
    )


def _split_front_matter(raw: str) -> tuple[str | None, str | None]:
    """Split ``raw`` into ``(front_matter, body)``.

    Returns ``(None, raw)`` when ``raw`` does not begin with a
    ``---``-fenced front-matter block — callers then fall through to
    heading / filename resolution.
    """
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        return None, raw
    return match.group(1), match.group(2)


def _parse_simple_yaml(text: str) -> dict[str, str]:
    """Parse the subset of YAML we need: ``key: value`` per line.

    No nested keys, no lists, no quoting. This deliberately avoids a
    PyYAML dependency for the Phase 3 MVP — front-matter expectations
    are narrow (``summary`` + ``source_ref``). When richer needs emerge,
    swap in PyYAML behind the same function signature.

    Blank lines and ``#`` comment lines are skipped. Lines without a
    colon are also skipped (rather than raising) so a stray note line
    does not abort the whole parse.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        out[key.strip()] = value.strip()
    return out


def _first_heading(text: str) -> str | None:
    """Return the text of the first H1 heading in ``text``, or ``None``."""
    match = _HEADING_RE.search(text)
    if match:
        return match.group(1)
    return None


def _summary_from_filename(path: Path) -> str:
    """Derive a human-readable summary from the file stem.

    ``review-pr-99.md`` → ``"review pr 99"``.
    ``my_inbox_item.md`` → ``"my inbox item"``.
    Falls back to the raw stem if collapsing separators leaves the
    string empty (unlikely, but keeps the return type non-empty).
    """
    stem = path.stem
    collapsed = stem.replace("-", " ").replace("_", " ").strip()
    return collapsed or stem
