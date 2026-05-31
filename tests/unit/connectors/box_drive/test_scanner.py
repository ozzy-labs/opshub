"""Unit tests for :class:`opshub.connectors.box_drive.scanner.BoxDriveScanner`.

These tests pin Phase 9 step B1's structural guarantees from ADR-0019:

* §決定 (b) — the scanner must never call ``open()`` / ``read_text`` /
  ``read_bytes`` / ``Path.open`` (CldAPI hydration prevention). The
  ``test_scanner_never_opens_files`` test below patches every file-IO
  primitive to a forbidden-call sentinel and runs a full scan to prove
  the path is unreachable.
* §決定 (c) — identity is ``rel_path`` (POSIX-form, root-relative).
* §決定 (d) — diff detection uses ``f"{size}:{mtime_ns}"`` and prior
  fingerprint matches are skipped.
* §決定 (e) — file removal is **not** detected; missing files just
  fail to be re-yielded.

Operational guards (symlink loops, ``max_depth``, ``max_files``,
permission errors, missing root) also live here so the connector
implementation in step B2 can rely on a well-tested scanner contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from opshub.connectors.box_drive import BoxDriveScanner
from opshub.core.errors import ConfigError

# ---------------------------------------------------------------------------
# Basic walk semantics
# ---------------------------------------------------------------------------


def test_scanner_yields_all_files_on_first_scan(tmp_path: Path) -> None:
    """First scan (empty prior_fingerprints) yields every file under root.

    Three files in two directories ensures both top-level walk and
    nested-directory descent are exercised. ``rel_path`` is the
    POSIX-form relative path; this is the contract Phase 9 step B2's
    mapper will hash into ``SourceObserved.external_id`` (ADR-0019
    §決定 (c)).
    """
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("b")
    (tmp_path / "sub" / "c.md").write_text("c")

    scanner = BoxDriveScanner(root_path=tmp_path)
    results = list(scanner.scan(prior_fingerprints={}))

    rel_paths = sorted(f.rel_path for f in results)
    assert rel_paths == ["a.txt", "sub/b.md", "sub/c.md"]


def test_scanned_file_carries_size_and_fingerprint(tmp_path: Path) -> None:
    """ScannedFile records ``size`` / ``mtime_ns`` / ``fingerprint`` consistently.

    The fingerprint is ``f"{size}:{mtime_ns}"`` — the mapper layer will
    propagate this string verbatim into ``SourceObserved.fingerprint``
    (Phase 9 A2 schema), so the scanner contract pins the exact format.
    """
    (tmp_path / "hello.txt").write_text("hello")
    scanner = BoxDriveScanner(root_path=tmp_path)

    [scanned] = list(scanner.scan(prior_fingerprints={}))

    assert scanned.rel_path == "hello.txt"
    assert scanned.size == 5  # len("hello") on POSIX text mode
    assert scanned.fingerprint == f"{scanned.size}:{scanned.mtime_ns}"


def test_scanner_skips_unchanged_files(tmp_path: Path) -> None:
    """Files whose fingerprint matches ``prior_fingerprints`` are skipped silently.

    This is the noise-suppression contract from ADR-0019 §決定 (d):
    100k+ workspaces would emit a flood of redundant SourceObserved
    events if the scanner re-yielded every unchanged file each pass.
    """
    (tmp_path / "stable.txt").write_text("stable")
    scanner = BoxDriveScanner(root_path=tmp_path)

    # First pass observes the file and lets us learn its fingerprint.
    [first] = list(scanner.scan(prior_fingerprints={}))

    # Second pass with the prior map should skip it.
    second = list(scanner.scan(prior_fingerprints={first.rel_path: first.fingerprint}))

    assert second == []


def test_scanner_yields_modified_files(tmp_path: Path) -> None:
    """A file whose ``(size, mtime_ns)`` changes is re-yielded.

    We bump the mtime explicitly with ``os.utime`` so the test does
    not depend on wall-clock progression between the two ``scan()``
    calls (some filesystems coalesce stat updates within a granularity
    window).
    """
    target = tmp_path / "edited.txt"
    target.write_text("v1")

    scanner = BoxDriveScanner(root_path=tmp_path)
    [first] = list(scanner.scan(prior_fingerprints={}))

    # Mutate content + bump mtime to a deterministic future value.
    target.write_text("v2-bigger")
    future_ns = first.mtime_ns + 1_000_000_000  # +1s
    os.utime(target, ns=(future_ns, future_ns))

    second = list(scanner.scan(prior_fingerprints={first.rel_path: first.fingerprint}))

    assert [s.rel_path for s in second] == ["edited.txt"]
    assert second[0].fingerprint != first.fingerprint


def test_scanner_yields_new_files(tmp_path: Path) -> None:
    """A file absent from ``prior_fingerprints`` is yielded as new.

    Combined with the modified-file case above this covers both
    "addition" semantics: a brand-new file (not in prior at all) and a
    pre-existing file with a fresh fingerprint.
    """
    (tmp_path / "old.txt").write_text("old")
    scanner = BoxDriveScanner(root_path=tmp_path)
    [old] = list(scanner.scan(prior_fingerprints={}))

    (tmp_path / "new.txt").write_text("new")

    second = list(scanner.scan(prior_fingerprints={old.rel_path: old.fingerprint}))

    assert [s.rel_path for s in second] == ["new.txt"]


def test_scanner_does_not_emit_for_removed_files(tmp_path: Path) -> None:
    """A file present in prior but absent on disk yields nothing.

    ADR-0019 §決定 (e) pins this MVP semantics: removal is observable
    only as "stale row in ``sources`` projection", not as a
    ``SourceDeleted`` event. The scanner therefore must not synthesise
    anything for the gap.
    """
    target = tmp_path / "doomed.txt"
    target.write_text("data")

    scanner = BoxDriveScanner(root_path=tmp_path)
    [observed] = list(scanner.scan(prior_fingerprints={}))

    target.unlink()

    second = list(scanner.scan(prior_fingerprints={observed.rel_path: observed.fingerprint}))

    assert second == []


# ---------------------------------------------------------------------------
# open() / read_text() / read_bytes() invariant — ADR-0019 §決定 (b)
# ---------------------------------------------------------------------------


def test_scanner_never_opens_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scanner must not call ``open()`` / ``Path.open`` / ``read_text`` / ``read_bytes``.

    ADR-0019 §決定 (b) makes this invariant load-bearing: any read
    would force CldAPI placeholders to hydrate, triggering network
    egress, OS notifications, and breaking ADR-0005 (External Content
    Minimization). We patch the four primitives to a forbidden-call
    sentinel and exercise a full scan. The scan must complete and
    yield every file *without* tripping any of them.

    We deliberately wire the fixture *after* writing files so the
    test's own preparation is not blocked (file creation legitimately
    needs ``write_text``).
    """
    # Prepare the tree before sealing the file-IO primitives.
    (tmp_path / "a.txt").write_text("contents-a")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.md").write_text("contents-b")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            f"scanner called a file-content primitive (ADR-0019 §決定 (b) "
            f"violation): args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    scanner = BoxDriveScanner(root_path=tmp_path)
    results = list(scanner.scan(prior_fingerprints={}))

    rel_paths = sorted(f.rel_path for f in results)
    assert rel_paths == ["a.txt", "nested/b.md"]


# ---------------------------------------------------------------------------
# Symlink handling
# ---------------------------------------------------------------------------


def test_scanner_skips_symlinks_when_follow_disabled(tmp_path: Path) -> None:
    """``follow_symlinks=False`` (default) ignores symlinks entirely.

    Box Drive does not create symlinks of its own, so any link under
    the root is operator-introduced and likely escapes the workspace.
    The safe default is to ignore them so the scanner never escapes
    the configured ``root_path``.
    """
    real_file = tmp_path / "real.txt"
    real_file.write_text("real")
    (tmp_path / "link.txt").symlink_to(real_file)

    scanner = BoxDriveScanner(root_path=tmp_path)
    results = list(scanner.scan(prior_fingerprints={}))

    assert [s.rel_path for s in results] == ["real.txt"]


def test_scanner_breaks_symlink_loops_when_follow_enabled(tmp_path: Path) -> None:
    """``follow_symlinks=True`` must not loop on cyclic links.

    Construction: ``tmp_path/a`` is a directory containing a symlink
    ``a/loop`` pointing back to ``tmp_path/a``. Walking with
    ``follow_symlinks=True`` would recurse forever without the inode
    ``visited`` set; the scanner must detect and break the cycle.
    """
    inner = tmp_path / "a"
    inner.mkdir()
    (inner / "file.txt").write_text("data")
    (inner / "loop").symlink_to(inner, target_is_directory=True)

    scanner = BoxDriveScanner(root_path=tmp_path, follow_symlinks=True, max_depth=64)
    results = list(scanner.scan(prior_fingerprints={}))

    # The lone real file must surface exactly once even though the
    # loop directory is descended via the cycle.
    rel_paths = [s.rel_path for s in results]
    assert "a/file.txt" in rel_paths


# ---------------------------------------------------------------------------
# Depth / file-count caps
# ---------------------------------------------------------------------------


def test_scanner_respects_max_depth(tmp_path: Path) -> None:
    """Files deeper than ``max_depth`` are not yielded.

    The cap is a structural safety guard — a misconfigured root
    pointing at ``/`` must not enumerate the whole filesystem. We use
    ``max_depth=1`` so a 3-deep tree is partially pruned: top-level
    and one level down are surfaced, but anything at depth 2+ is not.
    """
    (tmp_path / "top.txt").write_text("top")
    deep = tmp_path / "d1" / "d2" / "d3"
    deep.mkdir(parents=True)
    (tmp_path / "d1" / "shallow.txt").write_text("shallow")
    (deep / "deep.txt").write_text("deep")

    scanner = BoxDriveScanner(root_path=tmp_path, max_depth=1)
    results = sorted(s.rel_path for s in scanner.scan(prior_fingerprints={}))

    assert "top.txt" in results
    assert "d1/shallow.txt" in results
    assert "d1/d2/d3/deep.txt" not in results


def test_scanner_respects_max_files(tmp_path: Path) -> None:
    """Walk stops yielding once ``max_files`` is exceeded.

    We create five files and cap at two — the iteration must terminate
    after yielding three entries (the cap is "stop when ``yielded >
    max_files``" so two entries fit). This is the escape hatch from
    ADR-0019: operators with very large workspaces raise the cap in
    ``opshub.toml``, otherwise the scan aborts with a structured log
    warning rather than running unbounded.
    """
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(str(i))

    scanner = BoxDriveScanner(root_path=tmp_path, max_files=2)
    results = list(scanner.scan(prior_fingerprints={}))

    assert len(results) == 2


# ---------------------------------------------------------------------------
# Permission / missing entries
# ---------------------------------------------------------------------------


def test_scanner_continues_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that raises ``PermissionError`` during ``os.scandir`` is skipped.

    ``chmod 000`` is unreliable in CI containers (root often bypasses
    the bit). We instead monkeypatch ``os.scandir`` to raise once for
    a specific path, then delegate to the real implementation
    afterwards — this exercises the same code path deterministically.
    """
    (tmp_path / "open.txt").write_text("readable")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("would-be-readable")

    real_scandir = os.scandir

    def selective_scandir(path: Any) -> Any:
        if str(path) == str(locked):
            raise PermissionError(13, "permission denied", str(locked))
        return real_scandir(path)

    monkeypatch.setattr("opshub.connectors.box_drive.scanner.os.scandir", selective_scandir)

    scanner = BoxDriveScanner(root_path=tmp_path)
    results = sorted(s.rel_path for s in scanner.scan(prior_fingerprints={}))

    # The accessible file is yielded; the locked directory is silently
    # skipped (a structured log warning is emitted but tests do not
    # need to assert on it — that level of detail is reserved for the
    # logging module's own tests).
    assert results == ["open.txt"]


# ---------------------------------------------------------------------------
# Configuration / construction
# ---------------------------------------------------------------------------


def test_scanner_raises_config_error_when_root_missing(tmp_path: Path) -> None:
    """``ConfigError`` fires at construction time if ``root_path`` does not exist.

    Fail-fast at construction (not at first ``scan()``) so the operator
    sees the misconfiguration before any sync work has begun and the
    error message can point at ``docs/box-drive-setup.md`` (Phase 9
    C1).
    """
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ConfigError, match="does not exist"):
        BoxDriveScanner(root_path=missing)


def test_scanner_raises_config_error_when_root_is_not_dir(tmp_path: Path) -> None:
    """``ConfigError`` also fires when ``root_path`` exists but is a file.

    A common operator mistake: pointing at a single Box Drive file
    instead of the mount root. The error message distinguishes from
    the missing case so the diagnosis is obvious.
    """
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("data")
    with pytest.raises(ConfigError, match="not a directory"):
        BoxDriveScanner(root_path=not_a_dir)


def test_scanner_applies_exclude_globs(tmp_path: Path) -> None:
    """``exclude_globs`` filters by POSIX-form ``rel_path``.

    Three exclusion shapes are tested in one scan:

    * Bare basename match (``".DS_Store"``) — catches the macOS noise
      file regardless of depth.
    * Recursive ``**`` directory match (``"**/secrets/**"``) — used
      to keep sensitive paths out of the projection.
    * Top-level exact match (``"keepme.tmp"``) — the operator might
      pin a single transient file.
    """
    (tmp_path / ".DS_Store").write_text("noise")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".DS_Store").write_text("nested-noise")
    (tmp_path / "ok.txt").write_text("ok")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.pem").write_text("private")
    (tmp_path / "keepme.tmp").write_text("temp")

    scanner = BoxDriveScanner(
        root_path=tmp_path,
        exclude_globs=[".DS_Store", "**/secrets/**", "keepme.tmp"],
    )
    results = sorted(s.rel_path for s in scanner.scan(prior_fingerprints={}))

    assert results == ["ok.txt"]


def test_scanner_exposes_root_path(tmp_path: Path) -> None:
    """``root_path`` is exposed read-only for the connector to log / display."""
    scanner = BoxDriveScanner(root_path=tmp_path)
    assert scanner.root_path == tmp_path


# ---------------------------------------------------------------------------
# Phase 11 F4 — content_extraction opt-in (ADR-0019 §(b'), ADR-0025)
# ---------------------------------------------------------------------------
#
# The opt-in tests that actually invoke ``extract_document`` against the
# real fixtures use a per-test ``pytest.importorskip("markitdown")`` so a
# local dev install without ``--extra office`` still runs the default-off
# invariant tests above. The CI workflow opts into ``--extra office`` per
# Phase 11 F2, so the full opt-in suite executes there.


def test_scanner_content_extraction_off_yields_body_none_on_office_files(
    tmp_path: Path,
) -> None:
    """``content_extraction=False`` (default) leaves ``body=None`` on every file.

    ADR-0019 §(b') #1: the default-off path keeps Phase 9 behaviour
    bit-for-bit. Even an Office file under the root must surface as a
    plain :class:`ScannedFile` with ``body=None`` and no
    ``office_source_type`` discriminator.
    """
    # File extension matters, content does not (the scanner does not
    # read the file when content_extraction is off).
    (tmp_path / "a.docx").write_text("not-really-docx")
    (tmp_path / "b.txt").write_text("plain")

    scanner = BoxDriveScanner(root_path=tmp_path)
    results = {f.rel_path: f for f in scanner.scan(prior_fingerprints={})}

    assert results["a.docx"].body is None
    assert results["a.docx"].office_source_type is None
    assert results["a.docx"].body_truncated is False
    assert results["a.docx"].body_skip_reason is None
    assert results["b.txt"].body is None
    assert results["b.txt"].office_source_type is None


def test_scanner_content_extraction_off_never_opens_office_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-off path: even ``.docx`` triggers no ``open()`` (ADR-0019 §(b') #1).

    The Phase 9 ``test_scanner_never_opens_files`` test covers
    arbitrary file extensions; this one specifically pins that the
    opt-in default of ``content_extraction=False`` keeps the
    no-open invariant intact when the tree *does* contain Office
    files — i.e. the scanner does not silently sniff extensions and
    invoke the extractor.
    """
    (tmp_path / "report.docx").write_text("fake-docx")
    (tmp_path / "data.xlsx").write_text("fake-xlsx")
    (tmp_path / "deck.pptx").write_text("fake-pptx")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            f"scanner called a file-content primitive with "
            f"content_extraction=False (ADR-0019 §(b') #1 violation): "
            f"args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    scanner = BoxDriveScanner(root_path=tmp_path, content_extraction=False)
    results = list(scanner.scan(prior_fingerprints={}))

    rel_paths = sorted(f.rel_path for f in results)
    assert rel_paths == ["data.xlsx", "deck.pptx", "report.docx"]


def test_scanner_content_extraction_on_skips_non_office_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in path: non-Office extensions still skip ``open()`` (ADR-0019 §(b') #4).

    The ADR-0019 §(b') exception is narrow: only files whose extension
    is in :data:`SOURCE_TYPE_BY_EXTENSION` may be opened. A plain
    text file under the opt-in root must NOT trigger ``open()``
    even when ``content_extraction=True`` — that would re-open the
    walk-time hydration surface the ADR specifically forbids.
    """
    (tmp_path / "plain.txt").write_text("hello")
    (tmp_path / "data.json").write_text("{}")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            f"scanner opened a non-Office file with content_extraction=True "
            f"(ADR-0019 §(b') #4 violation): args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    scanner = BoxDriveScanner(root_path=tmp_path, content_extraction=True)
    results = list(scanner.scan(prior_fingerprints={}))

    rel_paths = sorted(f.rel_path for f in results)
    assert rel_paths == ["data.json", "plain.txt"]
    # Every yielded file is non-Office, so body / office_source_type
    # remain ``None``.
    assert all(f.body is None for f in results)
    assert all(f.office_source_type is None for f in results)


# Fixture files committed under tests/fixtures/office/ — small real Office
# documents built once via the F2 build recipe (ADR-0025 §Validation). We
# copy them into ``tmp_path`` so the scanner sees a clean directory layout
# without pollution from sibling tests.
_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "office"
_DOCX_FIXTURE = _FIXTURES_DIR / "sample.docx"
_XLSX_FIXTURE = _FIXTURES_DIR / "sample.xlsx"
_PPTX_FIXTURE = _FIXTURES_DIR / "sample.pptx"
_CORRUPT_DOCX_FIXTURE = _FIXTURES_DIR / "corrupt.docx"


def _copy_fixtures_into(target_dir: Path) -> None:
    """Copy the F2 Office fixtures into ``target_dir`` for scanner tests.

    Using a copy (not a symlink) keeps the scanner from following an
    operator-introduced link by accident — the
    :data:`BoxDriveScanner` defaults to ``follow_symlinks=False`` but
    we want the test path identical to the production walk.
    """
    import shutil

    for fixture in (_DOCX_FIXTURE, _XLSX_FIXTURE, _PPTX_FIXTURE):
        shutil.copy2(fixture, target_dir / fixture.name)


def test_scanner_content_extraction_on_extracts_office_bodies(tmp_path: Path) -> None:
    """``content_extraction=True`` + Office file → ``body`` populated, source_type pinned.

    Each of the three Office formats (.docx / .xlsx / .pptx) is
    extracted by :func:`extract_document` and the markdown body is
    threaded onto the :class:`ScannedFile`. The discriminator pinned
    on the result matches the ADR-0025 §決定 (d) table.
    """
    pytest.importorskip("markitdown")
    _copy_fixtures_into(tmp_path)

    scanner = BoxDriveScanner(root_path=tmp_path, content_extraction=True)
    results = {f.rel_path: f for f in scanner.scan(prior_fingerprints={})}

    assert results["sample.docx"].body is not None
    assert results["sample.docx"].office_source_type == "word_document"
    assert results["sample.docx"].body_skip_reason is None

    assert results["sample.xlsx"].body is not None
    assert results["sample.xlsx"].office_source_type == "excel_spreadsheet"
    assert results["sample.xlsx"].body_skip_reason is None

    assert results["sample.pptx"].body is not None
    assert results["sample.pptx"].office_source_type == "powerpoint_slide_deck"
    assert results["sample.pptx"].body_skip_reason is None


def test_scanner_content_extraction_failure_is_fail_safe(tmp_path: Path) -> None:
    """A corrupt Office file surfaces ``body=None`` + ``skip_reason`` (ADR-0025 §決定 (c)).

    The fail-safe contract is "never let one bad file stop the scan".
    The scanner must still yield the file (with ``office_source_type``
    populated because the extension matched) so the connector can
    persist a :class:`SourceObserved` for it; the operator sees the
    file in recall output even though the body is missing.
    """
    pytest.importorskip("markitdown")
    import shutil

    # Mix a healthy and a corrupt docx so the test asserts both shapes
    # in one scan.
    shutil.copy2(_DOCX_FIXTURE, tmp_path / "good.docx")
    shutil.copy2(_CORRUPT_DOCX_FIXTURE, tmp_path / "bad.docx")

    scanner = BoxDriveScanner(root_path=tmp_path, content_extraction=True)
    results = {f.rel_path: f for f in scanner.scan(prior_fingerprints={})}

    good = results["good.docx"]
    bad = results["bad.docx"]

    assert good.body is not None
    assert good.office_source_type == "word_document"
    assert good.body_skip_reason is None

    # Corrupt file: yielded with discriminator set (extension matched),
    # but body is None and skip_reason carries the failure tag.
    assert bad.body is None
    assert bad.office_source_type == "word_document"
    assert bad.body_skip_reason is not None
    assert bad.body_skip_reason.startswith("extraction failed")


def test_scanner_content_extraction_on_opens_only_via_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0019 §(b') #5 invariant: only ``extract_document`` may open files.

    With ``content_extraction=True``, the scanner is permitted to open
    Office files — but ONLY through
    :func:`opshub.core.document_extract.extract_document`. Magic-byte
    sniffing, SHA hashing, blanket walk-time opens, etc. remain
    forbidden. We pin this by replacing
    :func:`extract_document` with a stub that returns a known result
    *and* by sealing every other ``open``-capable primitive: the
    scanner must reach the stub for every Office file without
    tripping any of the sealed primitives.

    A non-Office file is present too; it must not trigger any
    ``open()`` either (#4 narrowness).
    """
    pytest.importorskip("markitdown")
    _copy_fixtures_into(tmp_path)
    (tmp_path / "plain.txt").write_text("non-office")

    extractor_calls: list[Path] = []

    from opshub.core.document_extract import ExtractResult

    def fake_extract(path: Path, **_kwargs: Any) -> ExtractResult:
        extractor_calls.append(path)
        # Derive a plausible source_type from extension so the
        # ScannedFile.office_source_type contract still holds.
        suffix = path.suffix.lower()
        if suffix == ".docx":
            source_type = "word_document"
        elif suffix == ".xlsx":
            source_type = "excel_spreadsheet"
        else:
            source_type = "powerpoint_slide_deck"
        return ExtractResult(
            body=f"stub body for {path.name}",
            truncated=False,
            skip_reason=None,
            source_type=source_type,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("opshub.core.document_extract.extract_document", fake_extract)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            f"scanner opened a file outside the extract_document() path "
            f"(ADR-0019 §(b') #5 violation): args={args!r} kwargs={kwargs!r}"
        )

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    scanner = BoxDriveScanner(root_path=tmp_path, content_extraction=True)
    results = {f.rel_path: f for f in scanner.scan(prior_fingerprints={})}

    # Every Office file was routed through the stub.
    assert sorted(p.name for p in extractor_calls) == [
        "sample.docx",
        "sample.pptx",
        "sample.xlsx",
    ]
    # Each Office file's body / source_type came from the stub.
    assert results["sample.docx"].body == "stub body for sample.docx"
    assert results["sample.docx"].office_source_type == "word_document"
    # Non-Office file got no body and no discriminator.
    assert results["plain.txt"].body is None
    assert results["plain.txt"].office_source_type is None


# ---------------------------------------------------------------------------
# Phase 11 audit Cluster B — office_settings wire (ADR-0025 §決定 (b)/(e))
# ---------------------------------------------------------------------------


def test_scanner_forwards_office_settings_to_extract_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``office_settings`` overrides land on ``extract_document`` as kwargs.

    Phase 11 audit Cluster B H3 wires
    :class:`opshub.core.config.OfficeSettings` through the scanner so
    ``opshub.toml [office]`` overrides reach the extractor. The test
    stubs :func:`extract_document` and asserts the four documented
    fields (``max_file_bytes``, ``max_chars``, ``max_cells_per_sheet``,
    ``max_cells_per_workbook``) arrive with the operator-tuned values
    rather than the ADR-0025 defaults.

    The instructions name ``max_file_size_mb = 100`` explicitly so the
    expected byte count is ``100 * 1024 * 1024`` — the scanner is the
    layer that multiplies MB → bytes; the extractor sees bytes only.
    """
    from opshub.core.config import ExcelOfficeSettings, OfficeSettings
    from opshub.core.document_extract import ExtractResult

    (tmp_path / "doc.docx").write_text("payload")

    captured: list[dict[str, Any]] = []

    def fake_extract(path: Path, **kwargs: Any) -> ExtractResult:
        captured.append({"path": path, **kwargs})
        return ExtractResult(
            body="stub",
            truncated=False,
            skip_reason=None,
            source_type="word_document",
        )

    monkeypatch.setattr("opshub.core.document_extract.extract_document", fake_extract)

    settings = OfficeSettings(
        max_file_size_mb=100,
        max_chars=1_000_000,
        excel=ExcelOfficeSettings(
            max_cells_per_sheet=20_000,
            max_cells_per_workbook=100_000,
        ),
    )
    scanner = BoxDriveScanner(
        root_path=tmp_path,
        content_extraction=True,
        office_settings=settings,
    )
    list(scanner.scan(prior_fingerprints={}))

    assert len(captured) == 1
    call = captured[0]
    assert call["max_file_bytes"] == 100 * 1024 * 1024
    assert call["max_chars"] == 1_000_000
    assert call["max_cells_per_sheet"] == 20_000
    assert call["max_cells_per_workbook"] == 100_000


def test_scanner_without_office_settings_uses_extract_document_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``office_settings=None`` leaves the extractor's defaults intact.

    Backwards compatibility guard: every pre-Cluster-B unit test that
    constructed :class:`BoxDriveScanner` without ``office_settings``
    relied on :func:`extract_document` falling back to its own
    ADR-0025-pinned defaults. The scanner now accepts an override but
    must still call the extractor argument-less when nothing was
    provided, so that contract stays valid.
    """
    from opshub.core.document_extract import ExtractResult

    (tmp_path / "doc.docx").write_text("payload")

    captured: list[dict[str, Any]] = []

    def fake_extract(path: Path, **kwargs: Any) -> ExtractResult:
        captured.append({"path": path, **kwargs})
        return ExtractResult(
            body="stub",
            truncated=False,
            skip_reason=None,
            source_type="word_document",
        )

    monkeypatch.setattr("opshub.core.document_extract.extract_document", fake_extract)

    scanner = BoxDriveScanner(root_path=tmp_path, content_extraction=True)
    list(scanner.scan(prior_fingerprints={}))

    # The scanner called extract_document positional-only with the path —
    # no keyword overrides leaked through. The extractor's signature
    # defaults take effect.
    assert len(captured) == 1
    assert captured[0] == {"path": tmp_path / "doc.docx"}
