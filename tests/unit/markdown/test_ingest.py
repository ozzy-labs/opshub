"""Unit tests for :mod:`opshub.markdown.ingest`.

The parser is pure (file-read-only, no DB / network), so every branch
is exercised against files created under ``tmp_path``:

* Front-matter present (full / partial / body-less) → fields populated.
* No front-matter, H1 heading present → heading wins as summary.
* No front-matter, no heading → filename stem becomes summary.
* ``compute_file_hash`` determinism, sensitivity to edits, and
  streaming behaviour for a multi-MB file.
* :class:`InboxItemDraft` is frozen + slotted (defensive — protects C2
  from accidentally mutating drafts mid-pipeline).
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from opshub.markdown.ingest import (
    InboxItemDraft,
    compute_file_hash,
    parse_inbox_file,
)

# --------------------------------------------------------------------- parse_inbox_file


def test_parse_with_full_front_matter(tmp_path: Path) -> None:
    """Both ``summary`` and ``source_ref`` in front-matter + a body → all 4 fields populated."""
    path = tmp_path / "item.md"
    path.write_text(
        "---\n"
        "summary: Review PR #99\n"
        "source_ref: github:owner/repo#99\n"
        "---\n"
        "\n"
        "Body paragraph one.\n"
        "Body paragraph two.\n",
        encoding="utf-8",
    )

    draft = parse_inbox_file(path)

    assert draft.path == path
    assert draft.summary == "Review PR #99"
    assert draft.source_ref == "github:owner/repo#99"
    assert draft.body == "Body paragraph one.\nBody paragraph two."
    assert len(draft.content_hash) == 64  # SHA-256 hex digest length


def test_parse_with_summary_only_front_matter(tmp_path: Path) -> None:
    """Front-matter with ``summary`` but no ``source_ref`` → source_ref is None."""
    path = tmp_path / "item.md"
    path.write_text(
        "---\nsummary: A short note\n---\n\nSome body.\n",
        encoding="utf-8",
    )

    draft = parse_inbox_file(path)

    assert draft.summary == "A short note"
    assert draft.source_ref is None
    assert draft.body == "Some body."


def test_parse_with_empty_body_after_front_matter(tmp_path: Path) -> None:
    """Front-matter only, no body text → body is None."""
    path = tmp_path / "item.md"
    path.write_text(
        "---\nsummary: Header only\n---\n",
        encoding="utf-8",
    )

    draft = parse_inbox_file(path)

    assert draft.summary == "Header only"
    assert draft.body is None


def test_parse_no_front_matter_falls_back_to_first_heading(tmp_path: Path) -> None:
    """``# My heading`` + body → summary = heading text, body retained."""
    path = tmp_path / "item.md"
    path.write_text(
        "# My heading\n\nBody text here.\n",
        encoding="utf-8",
    )

    draft = parse_inbox_file(path)

    assert draft.summary == "My heading"
    # No front-matter means the whole file is the "body" from the parser's
    # perspective — heading line included. We assert that the heading line
    # is preserved verbatim and the trailing body line follows.
    assert draft.body is not None
    assert "# My heading" in draft.body
    assert "Body text here." in draft.body
    assert draft.source_ref is None


def test_parse_no_front_matter_no_heading_falls_back_to_filename(tmp_path: Path) -> None:
    """Plain text, no heading, no front-matter → summary derived from filename stem."""
    path = tmp_path / "review-pr-99.md"
    path.write_text(
        "Just a free-form note without any heading.\n",
        encoding="utf-8",
    )

    draft = parse_inbox_file(path)

    assert draft.summary == "review pr 99"
    assert draft.source_ref is None
    assert draft.body == "Just a free-form note without any heading."


def test_parse_filename_with_underscores_and_extension(tmp_path: Path) -> None:
    """``my_inbox_item.md`` → ``"my inbox item"`` (extension dropped, ``_`` normalised)."""
    path = tmp_path / "my_inbox_item.md"
    path.write_text("no heading either\n", encoding="utf-8")

    draft = parse_inbox_file(path)

    assert draft.summary == "my inbox item"


# --------------------------------------------------------------------- compute_file_hash


def test_compute_file_hash_is_deterministic(tmp_path: Path) -> None:
    """Same file content → identical hash across calls."""
    path = tmp_path / "stable.md"
    path.write_text("hello world\n", encoding="utf-8")

    first = compute_file_hash(path)
    second = compute_file_hash(path)

    assert first == second
    assert first == hashlib.sha256(b"hello world\n").hexdigest()


def test_compute_file_hash_differs_on_content_change(tmp_path: Path) -> None:
    """Rewriting the file produces a different hash."""
    path = tmp_path / "mutable.md"
    path.write_text("version one\n", encoding="utf-8")
    before = compute_file_hash(path)

    path.write_text("version two\n", encoding="utf-8")
    after = compute_file_hash(path)

    assert before != after


def test_compute_file_hash_handles_large_file(tmp_path: Path) -> None:
    """1 MiB file streams correctly: streamed digest matches a single-shot hashlib digest."""
    path = tmp_path / "big.bin"
    # Deterministic, non-trivial 1 MiB payload (1024 * 1024 bytes).
    payload = (b"abcdefghij" * 1024)[:1024] * 1024
    assert len(payload) == 1024 * 1024
    path.write_bytes(payload)

    streamed = compute_file_hash(path)
    one_shot = hashlib.sha256(payload).hexdigest()

    assert streamed == one_shot


# --------------------------------------------------------------------- InboxItemDraft shape


def test_inbox_item_draft_is_frozen_and_slotted(tmp_path: Path) -> None:
    """Mutation raises and ``__dict__`` is absent (frozen=True, slots=True)."""
    draft = InboxItemDraft(
        path=tmp_path / "x.md",
        summary="s",
        source_ref=None,
        body=None,
        content_hash="0" * 64,
    )

    with pytest.raises(FrozenInstanceError):
        draft.summary = "mutated"  # type: ignore[misc]

    assert not hasattr(draft, "__dict__")
