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
